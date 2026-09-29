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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aterag.extract.assembler import AnnotationBook, PatternBook, assemble
from aterag.extract.models import (
    ROLE_OTHER,
    ROLE_STIMULUS_RESPONSE,
    SRC_BLOCK,
    SRC_ENTITY,
    ExcludedItem,
    ExtractionResult,
    ModelNotIngested,
    ReviewItem,
    SectionKeywordNotFound,
    TestCondition,
)
from aterag.extract.selector import section_matches, select_sections
from aterag.extract.sieve import apply_sieve
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

    conditions: list[TestCondition] = []
    unresolved = 0
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
            review.append(
                ReviewItem(
                    kind="unresolved_text",
                    section_path=cond.section_path,
                    heading=cond.heading,
                    detail=f"{cond.req_id} {cond.title} 备注: {row.get('notes', '')}"[:300],
                    hint="规则库与标题语义均未切出条件; 可补 condition_patterns 或写人工注记",
                )
            )

    prose_skipped = 0
    if not profile.include_prose:
        prose_skipped = sum(
            1
            for b in blocks
            if section_matches(b.section_path, prefixes)
            and not b.tables
            and b.text.strip()
            and b.text.strip() not in {"-", "—"}
        )

    result = ExtractionResult(
        model_id=model_id,
        doc_version=doc_version,
        profile=profile.name,
        source=source,
        selection=selection,
        conditions=conditions,
        excluded=outcome.excluded,
        needs_review=review,
        stats={
            "rows_total": len(rows),
            "kept": len(conditions),
            "excluded": len(outcome.excluded),
            "excluded_by_field": outcome.reasons(),
            "priority_unclassified": outcome.unclassified_priority,
            "no_data_dims": outcome.no_data_histogram(),
            "with_input_condition": sum(1 for c in conditions if c.input_conditions),
            "with_output_condition": sum(1 for c in conditions if c.output_conditions),
            "annotated": sum(1 for c in conditions if _has_annotated(c)),
            "annotation_stale": sum(1 for c in conditions if "annotation_stale" in c.flags),
            "needs_review": len(review),
            "unresolved_text": unresolved,
            "prose_skipped": prose_skipped,
            "limit_kind_unmapped": sum(1 for c in conditions if "limit_kind_unmapped" in c.flags),
            **unmapped_stats,
        },
    )
    return result


def _has_annotated(cond: TestCondition) -> bool:
    return any(
        c.confidence == "annotated" for c in (*cond.input_conditions, *cond.output_conditions)
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
