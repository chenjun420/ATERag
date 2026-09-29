"""抽取编排: 档案加载 -> 章节选择 -> 条目剔除 -> 语义装配 -> 结果组装.

依赖纪律 (对齐 CI 上那次依赖清单失灵的教训):
  本包只允许 import config / registry / table_schema / entity_extract / psycopg / 标准库。
  禁止 import rag.service (拖入 qdrant_client) 与 mcp_server (拖入 mcp / lightrag)。
  这让抽取能力既能在板卡服务里在线调用, 也能在离线 CLI / CI 单测里跑, 而不牵动全栈。

数据来源双通道, 结果必须一致:
  blocks    —— 直接从 blocks.jsonl 重跑抽取 (离线、确定性、无需 DB)
  postgres  —— 读 RAG 实际落库的实体 (校验"入库的"与"能抽出的"没跑偏)
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aterag.extract.assembler import AnnotationBook, PatternBook, assemble
from aterag.extract.assess import (
    RuleBook,
    assess_conditions,
    summarize,
    to_review_items,
)
from aterag.extract.models import (
    ROLE_OTHER,
    ROLE_STIMULUS_RESPONSE,
    SRC_BLOCK,
    SRC_ENTITY,
    STATUS_APPROVED,
    ExcludedItem,
    ExtractionResult,
    ModelNotIngested,
    ReviewItem,
    SectionKeywordNotFound,
    TestCondition,
)
from aterag.extract.resolve import ReferenceSpec
from aterag.extract.scenarios import ScenarioRules, expand_scenarios
from aterag.extract.selector import section_matches, select_sections
from aterag.extract.sieve import apply_sieve
from aterag.extract.supplement import (
    MethodBook,
    render_descriptions,
    supplement_conditions,
)
from aterag.ingest.markdown_parser import Block

DEFAULT_PROFILES_PATH = "config/doc_profiles.yaml"
DEFAULT_PATTERNS_PATH = "config/condition_patterns.yaml"
DEFAULT_ANNOTATION_DIR = "config/annotations"
DEFAULT_BLOCKS_DIR = "rag_storage/blocks"

SRC_BLOCKS = "blocks"
SRC_POSTGRES = "postgres"

# 参与装配的行字段。
# 最后一组 (schema_cols) 是在部分表格里根本不存在的维度, 其 "-" 只是"该表没这列",
# 不是"该维度无数据", 不应计入 no_data 统计 (否则 66 个遥测条目会各报 6 个假缺口)。
_TABLE_SPECIFIC_COLS = ("subject", "signal_name", "signal_req", "range_text", "accuracy")

_ROW_FIELDS = (
    "req_id",
    "title",
    "section_path",
    "heading",
    "priority",
    "unit",
    "min",
    "typ",
    "max",
    "rail",
    "notes",
    "requirement_text",
    "model_id",
) + _TABLE_SPECIFIC_COLS


# ---------------- 档案 ----------------


@dataclass
class SectionPrior:
    role: str = ROLE_STIMULUS_RESPONSE
    limits_to: str = "output"
    note: str = ""


@dataclass
class DocProfile:
    name: str
    section_keywords: tuple[str, ...]
    exclude_words: tuple[str, ...] = ()
    include_prose: bool = False
    section_priors: Mapping[str, SectionPrior] = field(default_factory=dict)
    default_role: str = ROLE_STIMULUS_RESPONSE
    default_limits_to: str = "output"
    description: str = ""
    reference_markers: tuple[str, ...] = ()
    req_id_pattern: str = ""
    review_dispositions: tuple[Mapping[str, Any], ...] = ()

    def disposition_for(
        self, *, req_id: str = "", kind: str = "", section: str = "", match: str = ""
    ) -> Mapping[str, Any] | None:
        """查已评审处置。条目型按 req_id, 表格型按 (kind, 章节, 表头签名)。"""
        for d in self.review_dispositions:
            if req_id:
                if str(d.get("req_id", "")) == req_id:
                    return d
            elif (
                str(d.get("kind", "")) == kind
                and str(d.get("section", "")) == section
                and str(d.get("match", "")) == match
            ):
                return d
        return None

    def reference_spec(self) -> ReferenceSpec:
        return ReferenceSpec(markers=self.reference_markers, req_id_pattern=self.req_id_pattern)

    def prior_for(self, section_path: str) -> SectionPrior:
        """最长前缀匹配 (4.3.1 命中 4.3 的先验, 4.3 未配则用兜底)。"""
        best: tuple[int, SectionPrior] | None = None
        for sec, p in self.section_priors.items():
            if section_matches(section_path, [sec]) and (best is None or len(sec) > best[0]):
                best = (len(sec), p)
        return (
            best[1]
            if best
            else SectionPrior(role=self.default_role, limits_to=self.default_limits_to)
        )


@dataclass
class ProfileBook:
    profiles: Mapping[str, DocProfile]
    default_profile: str
    source_path: str = ""

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PROFILES_PATH) -> ProfileBook:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"文档档案不存在: {p}")
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        profiles: dict[str, DocProfile] = {}
        for name, spec in (doc.get("profiles") or {}).items():
            priors = {
                str(sec): SectionPrior(
                    role=str(p.get("role", ROLE_STIMULUS_RESPONSE)),
                    limits_to=str(p.get("limits_to", "output")),
                    note=str(p.get("note", "")),
                )
                for sec, p in (spec.get("section_priors") or {}).items()
            }
            profiles[name] = DocProfile(
                name=name,
                section_keywords=tuple(spec.get("section_keywords") or ()),
                exclude_words=tuple(spec.get("exclude_words") or ()),
                include_prose=bool(spec.get("include_prose", False)),
                section_priors=priors,
                default_role=str(spec.get("default_role", ROLE_STIMULUS_RESPONSE)),
                default_limits_to=str(spec.get("default_limits_to", "output")),
                description=str(spec.get("description", "")),
                reference_markers=tuple(spec.get("reference_markers") or ()),
                req_id_pattern=str(spec.get("req_id_pattern", "")),
                review_dispositions=tuple(spec.get("review_dispositions") or ()),
            )
        if not profiles:
            raise ValueError(f"档案未定义任何 profile: {p}")
        return cls(
            profiles=profiles,
            default_profile=str(doc.get("default_profile") or next(iter(profiles))),
            source_path=str(p),
        )

    def get(self, name: str | None) -> DocProfile:
        key = name or self.default_profile
        if key not in self.profiles:
            raise KeyError(f"未定义的 profile: {key} (已定义: {sorted(self.profiles)})")
        return self.profiles[key]


# ---------------- 数据来源 ----------------


def load_blocks(model_id: str, blocks_dir: str | Path = DEFAULT_BLOCKS_DIR) -> list[Block]:
    p = Path(blocks_dir) / f"{model_id}.jsonl"
    if not p.exists():
        raise ModelNotIngested(
            f"未找到 {p} —— 型号 {model_id} 尚未入库 (先跑 scripts/ingest_*.py), 不用空结果冒充"
        )
    out: list[Block] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out.append(
            Block(
                chunk_id=d["chunk_id"],
                heading=d["heading"],
                level=d["level"],
                parent_headings=d.get("parent_headings", []),
                section_path=d.get("section_path", ""),
                text=d.get("text", ""),
                tables=d.get("tables", []),
                is_table_block=d.get("is_table_block", False),
            )
        )
    return out


def rows_from_blocks(
    blocks: Sequence[Block],
    model_id: str,
    doc_version: str,
    prefixes: Sequence[str],
    schema_registry=None,
) -> tuple[list[dict[str, Any]], list[ReviewItem], dict[str, int]]:
    """离线确定性通道: 直接重跑实体抽取, 不碰数据库。"""
    from aterag.ingest.entity_extract import extract_from_blocks
    from aterag.ingest.table_schema import load_registry

    reg = schema_registry or load_registry()
    entities = extract_from_blocks(blocks, model_id, doc_version, registry=reg)
    rows = [
        {k: e.props.get(k) for k in _ROW_FIELDS}
        for e in entities
        if e.etype == "Requirement" and section_matches(e.props.get("section_path", ""), prefixes)
    ]

    # 选中章节内未被映射的表 -> needs_review (行在 blocks 里, 但没有实体)
    review: list[ReviewItem] = []
    unmapped_rows = 0
    for b in blocks:
        if not section_matches(b.section_path, prefixes):
            continue
        for t in b.tables:
            if not t:
                continue
            det = reg.detect(t[0], t[1:])
            if det.matched and not (det.schema and det.schema.reference):
                continue
            if det.schema and det.schema.meta:
                continue
            unmapped_rows += len(t) - 1
            review.append(
                ReviewItem(
                    kind="unmapped_table",
                    section_path=b.section_path,
                    heading=b.heading,
                    detail=det.signature,
                    hint=det.reason,
                    rows=len(t) - 1,
                )
            )
    return rows, review, {"unmapped_table_rows": unmapped_rows}


def rows_from_postgres(
    dsn: str,
    model_id: str,
    prefixes: Sequence[str],
) -> list[dict[str, Any]]:
    """RAG 实存通道: 读 PG 实体 (与 blocks 通道应产出同一批 req_id)。"""
    import psycopg

    sql = "SELECT props FROM aterag_entities WHERE model_id = %s AND etype = 'Requirement'"
    with psycopg.connect(dsn) as conn:
        raw = conn.execute(sql, (model_id,)).fetchall()
    rows = []
    for (props,) in raw:
        if not section_matches(str(props.get("section_path", "")), prefixes):
            continue
        rows.append({k: props.get(k) for k in _ROW_FIELDS})
    return rows


# ---------------- 编排 ----------------


def extract_test_conditions(
    model_id: str,
    *,
    doc_version: str = "",
    profile_name: str | None = None,
    profiles: ProfileBook | None = None,
    patterns: PatternBook | None = None,
    methods: MethodBook | None = None,
    assess_rules: RuleBook | None = None,
    scenario_rules: ScenarioRules | None = None,
    annotations: AnnotationBook | None = None,
    source: str = SRC_BLOCKS,
    dsn: str | None = None,
    blocks_dir: str | Path = DEFAULT_BLOCKS_DIR,
) -> ExtractionResult:
    """抽取某型号在选定章节内的产测条件 (输入条件 / 输出条件)。

    source:
      blocks   —— 从 blocks 侧车重跑抽取 (默认: 离线、确定性、无需 DB)
      postgres —— 读 RAG 落库实体 (需 dsn)
    """
    prof_book = profiles or ProfileBook.load()
    profile = prof_book.get(profile_name)
    book = patterns or PatternBook.load(DEFAULT_PATTERNS_PATH)
    method_book = methods or MethodBook.load()
    # 角色词表从档案的 section_priors 取, 不在代码里硬编码角色名 ——
    # 角色是文档档案的知识, 换产品线换档案而非换代码。
    role_vocab = frozenset(
        pr.role for p in prof_book.profiles.values() for pr in p.section_priors.values()
    )
    # fail-closed: 方法库与词表/角色不自洽时直接抛, 不降级为"没有补齐" ——
    # 静默降级会表现为"条件一直没补上", 却查不出是配置坏了。
    method_book.validate(book.kinds, role_vocab)
    method_book.validate_templates()
    assess_book = assess_rules or RuleBook.load(method_book.path)
    assess_book.validate()
    # 场景规则缺失时 fail-closed: 静默退化成"不拆场景"会让下游拿到含混判据,
    # 却没有任何报错指向配置缺失。
    scen_rules = scenario_rules or ScenarioRules.load()

    blocks = load_blocks(model_id, blocks_dir)
    selection = select_sections(blocks, profile.section_keywords)
    prefixes = selection.section_prefixes

    review: list[ReviewItem] = []
    unmapped_stats: dict[str, int] = {}
    if source == SRC_BLOCKS:
        rows, review, unmapped_stats = rows_from_blocks(blocks, model_id, doc_version, prefixes)
    elif source == SRC_POSTGRES:
        if not dsn:
            raise ValueError("source=postgres 需要 dsn")
        rows = rows_from_postgres(dsn, model_id, prefixes)
    else:
        raise ValueError(f"未知 source: {source!r} (可选: {SRC_BLOCKS} | {SRC_POSTGRES})")

    if not rows:
        raise ModelNotIngested(
            f"{model_id} 在章节 {prefixes} 内没有任何需求实体 —— "
            f"检查档案关键字是否匹配该文档的章节标题"
        )

    outcome = apply_sieve(rows, profile.exclude_words)
    one_sided: dict[str, list[str]] = {}  # 循环后填充 (见 stats)

    # 剔除清单与已评审处置在主循环之前建立: 两者都要被循环与收尾阶段共同写入
    excluded: list[ExcludedItem] = list(outcome.excluded)
    reviewed: list[ReviewItem] = []

    def _resolve_disposition(item: ReviewItem) -> bool:
        """已评审处置命中则把待审条目移出队列并留痕; 返回是否命中。"""
        d = profile.disposition_for(
            req_id=_req_id_in(item.detail),
            kind=item.kind,
            section=item.section_path,
            match=item.detail,
        )
        if d is None:
            return False
        if item in review:
            review.remove(item)
        reviewed.append(
            ReviewItem(
                kind=f"resolved:{d.get('verdict', 'decided')}",
                section_path=item.section_path,
                heading=item.heading,
                detail=item.detail[:200],
                hint=f"已评审不提取: {d.get('reason', '')}",
            )
        )
        return True

    conditions: list[TestCondition] = []
    unresolved = 0
    n_ann_draft = 0
    for row in outcome.kept:
        prior = profile.prior_for(str(row.get("section_path", "")))
        asm = assemble(
            row, role=prior.role, limits_to=prior.limits_to, book=book, annotations=annotations
        )
        cond = TestCondition(
            req_id=str(row.get("req_id", "")),
            title=str(row.get("title", "")),
            section_path=str(row.get("section_path", "")),
            heading=str(row.get("heading", "")),
            priority=str(row.get("priority", "")),
            rail=str(row.get("rail", "")),
            unit=str(row.get("unit", "")),
            notes=str(row.get("notes", "")),
            role=prior.role,
            input_conditions=asm.inputs,
            output_conditions=asm.outputs,
            limits={
                k: row.get(k)
                for k in ("min", "typ", "max", "unit", "rail")
                if row.get(k) is not None
            },
            flags=list(asm.flags),
            etype="Requirement",
            source=SRC_ENTITY if source == SRC_POSTGRES else SRC_BLOCK,
        )
        if asm.draft:
            # 未评审注记: 已应用 (语义更优) 但必须显式可见, 供评审清单与下游过滤
            n_ann_draft += 1
            cond.flags.append("annotation_draft")
        if not str(row.get("priority", "")).strip():
            cond.flags.append("priority_unclassified")
        # R5: 单元格短横线 = 该维度无数据 (不是"不要求"), 显式标注便于人工判断覆盖度
        no_data = outcome.no_data_dims.get(cond.req_id)
        if no_data:
            cond.flags.append("no_data:" + ",".join(no_data))
        conditions.append(cond)
        # 一条条件都没切出来 (且不是人工注记) -> 进待审队列, 不静默放过
        if not asm.inputs and not asm.outputs and not asm.annotated:
            unresolved += 1
            item = ReviewItem(
                kind="unresolved_text",
                section_path=cond.section_path,
                heading=cond.heading,
                detail=f"{cond.req_id} {cond.title} 备注: {row.get('notes', '')}"[:300],
                hint="规则库与标题语义均未切出条件; 可补 condition_patterns 或写人工注记",
            )
            review.append(item)
            _resolve_disposition(item)

    for it in list(review):
        _resolve_disposition(it)

    prose_skipped = 0
    prose_audit: list[ReviewItem] = []
    if not profile.include_prose:
        # 散文块审计: 区分"跨章节引用"与"真无要求"两类。引用类交给 resolve 穿透 ——
        # 不可一律当噪声跳过 (4.3.4.5 版本管理功能的正文就是一句"详见4.3.4.4",
        # 一律跳过等于让一条真实需求凭空消失)。
        from aterag.extract.resolve import find_references, resolve_references, to_conditions
        from aterag.ingest.table_schema import load_registry

        ref_spec = profile.reference_spec()
        if not ref_spec.enabled:
            # 显式关闭而非静默跳过: 状态进 stats, 上层可见
            ref_state = "disabled (档案未声明 reference_markers/req_id_pattern)"
            ref_counts = {"references": 0, "resolved": 0, "unresolved": 0}
        else:
            refs = find_references(blocks, ref_spec, section_prefixes=prefixes)
            if refs:
                refs = resolve_references(refs, blocks, load_registry(), ref_spec)
                for h in refs:
                    if not h.resolved:
                        prose_audit.append(
                            ReviewItem(
                                kind="unresolved_reference",
                                section_path=h.section_path,
                                heading=h.heading,
                                detail=f"{h.req_id} 引用 {h.target}: {h.note}",
                            )
                        )
                        continue
                    rc = to_conditions([h])[0]
                    hit_words = [
                        w
                        for w in profile.exclude_words
                        if any(w in o.text for o in rc.output_conditions)
                    ]
                    if hit_words:
                        # 被引用内容自身声明"无要求": 整条不产条件, 但留剔除记录
                        excluded.append(
                            ExcludedItem(
                                req_id=rc.req_id,
                                title=rc.title,
                                section_path=rc.section_path,
                                reason=f"引用穿透后内容命中剔除词 {hit_words}",
                                matched_word=hit_words[0],
                                field="reference_target",
                                notes=rc.notes,
                            )
                        )
                        continue
                    conditions.append(rc)
            n_res = sum(1 for h in refs if h.resolved)
            ref_state = "enabled"
            ref_counts = {
                "references": len(refs),
                "resolved": n_res,
                "unresolved": len(refs) - n_res,
            }
        prose_skipped = sum(
            1
            for b in blocks
            if section_matches(b.section_path, prefixes)
            and not b.tables
            and (b.text or "").strip()
            and (b.text or "").strip() not in {"-", "—"}
        )

    one_sided = _one_sided_stats(conditions, profile)

    # ---- A6/A6': 业界方法补齐 + 产测充分性评估 ----
    # 补齐只保证"有条件", 评估再判定"条件是否充分且必要"。两者都只增不改:
    # 规格书原有子句一律不动, 提案一律 draft, 结论一律交人工裁定。
    supp = supplement_conditions(conditions, method_book)
    render_descriptions(conditions, method_book.templates)
    assessments = assess_conditions(conditions, assess_book)
    for it in supp.needs_review:
        if it not in review:
            review.append(it)
    for it in to_review_items(assessments):
        if it not in review:
            review.append(it)
    assess_stats = summarize(assessments)

    # ---- A7 场景拆分 ----
    # 放在补齐与评估之后: 补齐后的条件才是拆场景的输入(场景绑定的是
    # "在什么条件下测", 含补齐出来的常识前提)。序号与标识唯一性由引擎内断言
    # 保证 —— case_code 由此派生, 冲突会让 P2 的幂等导入静默丢条件。
    scen_res = expand_scenarios(conditions, scen_rules)
    scenarios = scen_res.scenarios
    excluded_scenarios = scen_res.excluded

    result = ExtractionResult(
        model_id=model_id,
        doc_version=doc_version,
        profile=profile.name,
        source=source,
        selection=selection,
        conditions=conditions,
        excluded=outcome.excluded,
        needs_review=review + prose_audit,
        reviewed_dispositions=reviewed,
        assessments=assessments,
        scenarios=scenarios,
        excluded_scenarios=excluded_scenarios,
        stats={
            # rows_total/kept/excluded 只统计"表格行"这一来源, 保持 147 = 94 + 53 的恒等;
            # 引用穿透产出的散文需求单列, 否则对账会凭空多出一条而无法解释。
            "rows_total": len(rows),
            "kept": len(outcome.kept),
            "excluded": len(excluded),
            "conditions_total": len(conditions),
            "from_reference": len(conditions) - len(outcome.kept),
            "excluded_by_field": outcome.reasons(),
            "priority_unclassified": outcome.unclassified_priority,
            "no_data_dims": outcome.no_data_histogram(),
            "with_input_condition": sum(1 for c in conditions if c.input_conditions),
            "with_output_condition": sum(1 for c in conditions if c.output_conditions),
            "annotated": sum(1 for c in conditions if _has_annotated(c)),
            "annotation_draft": n_ann_draft,
            "annotation_stale": sum(1 for c in conditions if "annotation_stale" in c.flags),
            "needs_review": len(review) + len(prose_audit),
            "reviewed_dispositions": len(reviewed),
            "unresolved_text": unresolved,
            # 契约: 每个被抽出的需求都应同时有输入与输出条件 (被过滤的不参与)。
            # 达不到的必须带成因, 并区分"设计使然"与"待处置":
            #   by_design   章节先验导致 —— 规格书不会在输入特性里重述"输出正常", 不可修
            #   actionable  规则未覆盖 / 规格书无数据 —— 需补规则或人工处置
            "one_sided": one_sided,
            "one_sided_total": sum(len(v) for v in one_sided.values()),
            "one_sided_by_design": sum(
                len(v)
                for k, v in one_sided.items()
                if k.endswith((_CAUSE_BY_PRIOR_INPUT, _CAUSE_BY_PRIOR_OUTPUT))
            ),
            "one_sided_actionable": sum(
                len(v)
                for k, v in one_sided.items()
                if k.endswith((_CAUSE_RULES_MISSED, _CAUSE_NO_DATA, _CAUSE_SPEC_SINGLE))
            ),
            "prose_skipped": prose_skipped,
            # ---- A6 业界方法补齐 ----
            "supplemented": len(supp.supplemented),
            "supplement_needs_manual": len(supp.needs_review),
            "supplement_methods_used": len(supp.hits),
            "draft_clauses": sum(
                1
                for c in conditions
                for cl in (*c.input_conditions, *c.output_conditions)
                if cl.status != STATUS_APPROVED
            ),
            # ---- A6' 产测充分性评估 ----
            "assessed": assess_stats["assessed"],
            "assess_by_verdict": {k: len(v) for k, v in assess_stats["by_verdict"].items()},
            "assess_needs_decision": len(assess_stats["needs_decision"]),
            "assess_pending_signoff": len(assess_stats["pending_signoff"]),
            "descriptions_rendered": sum(1 for c in conditions if c.description),
            # ---- A7 场景拆分 ----
            "scenarios": len(scenarios),
            "scenarios_excluded": len(excluded_scenarios),
            "scenarios_named": sum(1 for s in scenarios if getattr(s, "name", "")),
            "scenario_tiers": len(scen_res.tiers),
            "reference_resolution": ref_state,
            "reference_hits": ref_counts["references"],
            "reference_resolved": ref_counts["resolved"],
            "reference_unresolved": ref_counts["unresolved"],
            "limit_kind_unmapped": sum(1 for c in conditions if "limit_kind_unmapped" in c.flags),
            **unmapped_stats,
        },
    )
    return result


def _req_id_in(text: str) -> str:
    """从待审详情串里取需求编号 (形如 'SR-PA601-D54A-1101 ...')。"""
    m = re.search(r"\b([A-Z]{2,6}[0-9A-Z]*(?:-[A-Z0-9]+)+)\b", str(text or ""))
    return m.group(1) if m else ""


# 单边条件成因 (诊断用; 判据来自数据结构, 不含具体文档词汇)
_CAUSE_NO_DATA = "spec_gives_no_data"  # 规格书该行本身无数据 (限值/备注全为 '-')
_CAUSE_SPEC_SINGLE = "spec_states_only_one_side"  # 规格书只写了单边
_CAUSE_RULES_MISSED = "rules_missed"  # 备注里有措辞但规则未覆盖
# 章节先验导致的对侧缺失: 规格书不会在"输入特性"里重述"输出正常",
# 也不会在"输出特性"里逐条写"额定输入" —— 这类单边是设计使然, 不是缺陷。
_CAUSE_BY_PRIOR_INPUT = "prior_input_domain"
_CAUSE_BY_PRIOR_OUTPUT = "prior_output_spec"


def _one_sided_stats(
    conditions: Sequence[TestCondition], profile: DocProfile
) -> dict[str, list[str]]:
    """按"缺哪一侧 + 成因"分组, 供人工判断是补规则还是确认规格书如此。

    键用 req_id + 电压轨: 同一需求编号常有多轨/多档位行 (SR-1203 有 3 行),
    仅按 req_id 会把不同轨混成一条, 掩盖真实的单边原因。

    关键: 章节先验 (limits_to) 决定了限值归哪一侧, 因此"缺另一侧"往往是
    先验的必然结果而非缺陷 —— 例如输入特性章节 limits_to=input, 其限值本就
    只构成激励, 响应侧是"正常工作"而规格书不会逐条重述。这类必须单列,
    否则统计会把正常设计当成漏洞去"修"。
    """
    out: dict[str, list[str]] = {}
    for c in conditions:
        missing = [
            side
            for side, cl in (("input", c.input_conditions), ("output", c.output_conditions))
            if not cl
        ]
        if not missing:
            continue
        ident = f"{c.req_id}@{c.rail}" if c.rail else c.req_id
        prior = profile.prior_for(c.section_path)
        has_limits = any(c.limits.get(k) is not None for k in ("min", "typ", "max"))
        has_text = bool(c.notes.strip()) and c.notes.strip() not in {"-", "—"}

        if not has_limits and not has_text:
            cause = _CAUSE_NO_DATA
        elif prior.limits_to == "input" and "output" in missing:
            # 输入特性: 限值即激励域, 响应侧(正常工作)规格书不逐条声明
            cause = _CAUSE_BY_PRIOR_INPUT
        elif prior.limits_to == "output" and "input" in missing:
            # 输出特性: 限值即响应判据, 激励侧(额定输入等)规格书不逐条声明
            cause = _CAUSE_BY_PRIOR_OUTPUT
        elif "input" in missing and has_text and not has_limits:
            cause = _CAUSE_RULES_MISSED
        else:
            cause = _CAUSE_SPEC_SINGLE
        out.setdefault(f"missing_{'+'.join(missing)}__{cause}", []).append(ident or c.title)
    return {k: sorted(set(v)) for k, v in out.items()}


def _has_annotated(cond: TestCondition) -> bool:
    """该条件是否由人工注记产出 (不论草稿/已签字 —— 草稿也是人工语义)。"""
    return any(
        c.confidence in {"annotated", "proposed"}
        for c in (*cond.input_conditions, *cond.output_conditions)
    )


def load_annotations(model_id: str, ann_dir: str | Path = DEFAULT_ANNOTATION_DIR) -> AnnotationBook:
    return AnnotationBook.load(Path(ann_dir) / f"{model_id}.conditions.yaml")


__all__ = [
    "DocProfile",
    "ProfileBook",
    "SectionPrior",
    "extract_test_conditions",
    "load_annotations",
    "load_blocks",
    "rows_from_blocks",
    "rows_from_postgres",
    "ExcludedItem",
    "SectionKeywordNotFound",
    "ModelNotIngested",
    "ROLE_OTHER",
]
