"""bundle 契约 (Pydantic) —— ATERag → ATEStudio 唯一的数据面格式。

为什么用 Pydantic 而不是手写 dict
----------------------------------
bundle 是两仓之间唯一的约定, 一旦字段对不上, 症状是"导入成功但条件少了一半"
这类静默故障, 而不是报错。手写 dict 没有任何校验, 字段拼错要等运行期才发现。
Pydantic 在两侧都能:
  * 导出 JSON Schema, 作为契约的机器可读定义;
  * 计算 contract_hash —— 两边各自算出同一个 hash 才能导入, 字段漂移当场拒收。

版本策略
--------
bundle_version 走整数主次 (1.0)。加可选字段= 次版本号, 加必填字段或删字段 = 主
版本号。**只允许向后兼容的演进** —— 1.x 的导入器必须能读 1.0 的 bundle, 否则
历史 bundle 会集体失效, 而它们是审计凭据, 不能作废。

字段红线
--------
1. 三桶全带 (conditions / excluded / review)。不因为"下游用不上"就省略 ——
   省略会让下游无法区分"没有"和"没传", 前者会显示为覆盖率缺口。
2. 状态忠实携带 (approved / draft)。不在导出时替下游做批准决定。
3. 指纹分层: cond / req / bundle / product_doc。
   product_doc 相同即可短路重抽, 省掉整轮抽取与导出。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 契约主版本。破坏性变更才递增。
CONTRACT_MAJOR = 1
CONTRACT_MINOR = 0
BUNDLE_VERSION = f"{CONTRACT_MAJOR}.{CONTRACT_MINOR}"

#: 子句/用例状态。与 extract.models 的 STATUS_* 对应, 但此处独立定义 ——
# 契约不应 import 对方的实现, 那会让"契约"变成"共享代码", 一改就两边都动。
ContractStatus = Literal["approved", "draft"]


class ClauseModel(BaseModel):
    """一条条件子句。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., min_length=1, max_length=64, description="封闭 kind 词表的键")
    text: str = Field("", max_length=2000, description="原文片段或方法注记, 溯源用")
    role: Literal["input", "output"]
    value: dict[str, Any] | None = None
    source: str = Field("notes", max_length=32)
    confidence: str = Field("rule", max_length=32)
    status: ContractStatus = "approved"
    method_ref: str = Field("", max_length=64, description="补齐该子句的方法 id")
    cond_fingerprint: str = Field("", max_length=64)


class LimitModel(BaseModel):
    """一条限值。min/typ/max 均可缺 —— 单边限值是规格书常见形态。"""

    model_config = ConfigDict(extra="forbid")

    rail: str = Field("", max_length=32)
    qualifier: str = Field("", max_length=64, description="长期/短期等工况限定")
    min: float | None = None
    typ: float | None = None
    max: float | None = None
    unit: str = Field("", max_length=32)


class ScenarioModel(BaseModel):
    """一个可执行条件组合 (需求 × 条件维度)。"""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(..., min_length=1, max_length=200)
    seq: int = Field(..., ge=0, description="需求内唯一序号; case_code 由 (req_id, seq) 派生")
    name: str = Field("", max_length=200, description="产测可读名称, 如 满载输出电流@-54V")
    rail: str = Field("", max_length=32)
    bindings: dict[str, str] = Field(default_factory=dict)
    derived: dict[str, float] = Field(default_factory=dict)
    basis: str = Field("", max_length=200)
    source: str = Field("spec", max_length=32)


class RequirementModel(BaseModel):
    """一条需求及其条件、场景、判据。"""

    model_config = ConfigDict(extra="forbid")

    requirement_code: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="唯一幂等键(与 product_code 组合)。同一规格编号有多行时以行限定词消歧",
    )
    spec_requirement_id: str = Field(
        "",
        max_length=100,
        description="规格书原编号(如 SR-xxx-1203)。同编号可能有多行(多轨/多工作制), "
        "此字段保持原样供展示与追溯, 不参与幂等",
    )
    title: str = Field(..., min_length=1, max_length=255)
    section_path: str = Field("", max_length=64)
    role: str = Field("", max_length=32)
    description: str = Field("", max_length=8000, description="输入/输出条件合成后的可读描述")
    notes: str = Field("", max_length=2000)
    req_fingerprint: str = Field("", max_length=64)
    flags: list[str] = Field(default_factory=list)
    input_conditions: list[ClauseModel] = Field(default_factory=list)
    output_conditions: list[ClauseModel] = Field(default_factory=list)
    limits: list[LimitModel] = Field(default_factory=list)
    scenarios: list[ScenarioModel] = Field(default_factory=list)
    one_sided: dict[str, Any] = Field(
        default_factory=dict, description="缺哪侧+成因, 双边齐全时为空"
    )
    assessment: dict[str, str] = Field(
        default_factory=dict, description="verdict / rule_id / basis"
    )

    @field_validator("scenarios")
    @classmethod
    def _seq_unique(cls, v: list[ScenarioModel]) -> list[ScenarioModel]:
        """需求内场景序号必须唯一。

        下游 case_code = f"{requirement_code}-S{seq:03d}", 序号撞车会让两条
        场景被导入成同一条用例, 条件静默丢失。这条约束必须在契约层就拦住,
        不能指望导入方去发现。
        """
        seqs = [s.seq for s in v]
        if len(seqs) != len(set(seqs)):
            dup = sorted({s for s in seqs if seqs.count(s) > 1})
            raise ValueError(f"场景序号重复: {dup} (会导致 case_code 冲突)")
        ids = [s.scenario_id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("场景标识重复")
        return v


class ExcludedModel(BaseModel):
    """被剔除项 —— 必带原因, 剔除本身要可审计。"""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field("", max_length=100)
    title: str = Field("", max_length=255)
    section_path: str = Field("", max_length=64)
    reason: str = Field("", max_length=500)
    matched_word: str = Field("", max_length=64)
    notes: str = Field("", max_length=2000)


class ReviewModel(BaseModel):
    """待审项 —— 状态与理由忠实带给下游, 由人决定。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., max_length=64)
    section_path: str = Field("", max_length=64)
    heading: str = Field("", max_length=255)
    detail: str = Field("", max_length=1000)
    hint: str = Field("", max_length=1000)
    method_ref: str = Field("", max_length=64)


class ExcludedScenarioModel(BaseModel):
    """被排除的不可行场景组合。"""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field("", max_length=100)
    title: str = Field("", max_length=255)
    bindings: dict[str, str] = Field(default_factory=dict)
    reason: str = Field("", max_length=200)
    rule_id: str = Field("", max_length=64)


class BundleModel(BaseModel):
    """完整 bundle —— 两仓之间唯一的对接面。"""

    model_config = ConfigDict(extra="forbid")

    bundle_version: str = Field(..., description="语义版本; 导入方须校验主版本")
    contract_hash: str = Field(..., max_length=64, description="契约定义指纹, 两侧互校")
    product_code: str = Field(..., min_length=1, max_length=100)
    doc_version: str = Field("", max_length=32)
    product_doc_fingerprint: str = Field(
        "", max_length=64, description="产品文档指纹; 相同即可短路重抽"
    )
    generated_at: str = Field("", max_length=64)
    generator: str = Field("", max_length=64)
    bundle_fingerprint: str = Field("", max_length=64)
    requirements: list[RequirementModel] = Field(default_factory=list)
    excluded: list[ExcludedModel] = Field(default_factory=list)
    review: list[ReviewModel] = Field(default_factory=list)
    excluded_scenarios: list[ExcludedScenarioModel] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    #: 相对上次导出的消失需求 —— 下游据此标 stale 而非物理删除
    removed_requirement_codes: list[str] = Field(default_factory=list)

    @field_validator("bundle_version")
    @classmethod
    def _version_ok(cls, v: str) -> str:
        try:
            major = int(v.split(".")[0])
        except (ValueError, IndexError) as e:
            raise ValueError(f"bundle_version 格式非法: {v!r}") from e
        if major != CONTRACT_MAJOR:
            raise ValueError(f"bundle 主版本不兼容: 导入方为 {CONTRACT_MAJOR}.x, 收到 {v}")
        return v

    @field_validator("requirements")
    @classmethod
    def _req_codes_unique(cls, v: list[RequirementModel]) -> list[RequirementModel]:
        codes = [r.requirement_code for r in v]
        if len(codes) != len(set(codes)):
            dup = sorted({c for c in codes if codes.count(c) > 1})
            raise ValueError(f"requirement_code 重复: {dup} (幂等键冲突)")
        return v


def contract_hash() -> str:
    """契约定义的指纹 —— 由 Pydantic 导出的 JSON Schema 计算。

    两侧各算一次, 不一致即拒收。这道闸挡的是"一边加了字段另一边没跟上":
    那类 bug 的症状是导入成功但数据缺失, 排查成本极高, 而这里一次比对就能拦下。

    只取结构本身参与 hash: title/description 这类说明文字改动不该让契约失效,
    否则每改一句注释就要同步两侧, 久了就没人同步了。
    """
    schema = BundleModel.model_json_schema()
    stable = _stable_repr(schema)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]


_VOLATILE_SCHEMA_KEYS = frozenset({"title", "description", "examples", "$comment"})


def _stable_repr(node: Any) -> str:
    """去掉易变文案后的稳定序列化, 保证两侧算出同一个 hash。"""
    if isinstance(node, Mapping):
        items = {k: v for k, v in node.items() if k not in _VOLATILE_SCHEMA_KEYS}
        return "{" + ",".join(f"{k}:{_stable_repr(items[k])}" for k in sorted(items)) + "}"
    if isinstance(node, list | tuple):
        return "[" + ",".join(_stable_repr(x) for x in node) + "]"
    return json.dumps(node, sort_keys=True, ensure_ascii=False)


def _fp(*parts: Any) -> str:
    """稳定指纹 —— 只取语义字段, 不含生成时间戳。

    不含时间戳是硬要求: 指纹若随时间变化, 同一份内容每次导出都算作"已变更",
    下游就会把全部用例重置为 draft, 评审状态被反复清空。
    """
    canon = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def clause_to_model(c: Any) -> ClauseModel:
    """ConditionClause -> ClauseModel。"""
    return ClauseModel(
        kind=c.kind,
        text=c.text or "",
        role=c.role,
        value=dict(c.value) if c.value else None,
        source=c.source or "notes",
        confidence=c.confidence or "rule",
        status=c.status or "approved",
        method_ref=getattr(c, "method_ref", "") or "",
        cond_fingerprint=_fp(
            c.kind, c.text, c.role, c.value, getattr(c, "method_ref", ""), c.status
        ),
    )


def requirement_to_model(
    cond: Any,
    scenarios: Sequence[Any] = (),
    assessment: Mapping[str, str] | None = None,
) -> RequirementModel:
    """TestCondition + 其场景 + 评估结论 -> RequirementModel。"""
    inputs = [clause_to_model(c) for c in getattr(cond, "input_conditions", ())]
    outputs = [clause_to_model(c) for c in getattr(cond, "output_conditions", ())]
    limits_src = getattr(cond, "limits", {}) or {}
    rail = getattr(cond, "rail", "") or ""
    unit = str(limits_src.get("unit", "") or "")
    limit = LimitModel(
        rail=rail,
        qualifier=_qualifier_of(cond),
        min=limits_src.get("min"),
        typ=limits_src.get("typ"),
        max=limits_src.get("max"),
        unit=unit,
    )
    scen_models = [
        ScenarioModel(
            scenario_id=s.scenario_id,
            seq=s.seq,
            name=s.name or "",
            rail=s.rail,
            bindings=dict(s.bindings),
            derived=dict(s.derived),
            basis=s.basis or "",
            source=s.source,
        )
        for s in scenarios
    ]
    missing = [side for side, cl in (("input", inputs), ("output", outputs)) if not cl]
    return RequirementModel(
        requirement_code=cond.req_id,
        title=cond.title or cond.req_id,
        section_path=getattr(cond, "section_path", "") or "",
        role=getattr(cond, "role", "") or "",
        description=getattr(cond, "description", "") or "",
        notes=getattr(cond, "notes", "") or "",
        req_fingerprint=_fp(
            cond.req_id,
            rail,
            cond.title,
            getattr(cond, "notes", ""),
            [c.model_dump() for c in inputs],
            [c.model_dump() for c in outputs],
            limit.model_dump(),
        ),
        flags=list(getattr(cond, "flags", ())),
        input_conditions=inputs,
        output_conditions=outputs,
        limits=[limit]
        if any(limit.model_dump()[k] is not None for k in ("min", "typ", "max"))
        else [],
        scenarios=scen_models,
        one_sided={"missing": missing} if missing else {},
        assessment=dict(assessment or {}),
    )


def _row_code(cond: Any, used: set[str]) -> str:
    """多行同编号的行级唯一键。

    消歧维度: 轨 + 限值形态, 不用原文备注也不用行号。
    * 用原文: 备注改一个错别字就换一批幂等键, 下游会把所有相关用例判成
      "已删除再新增", 追溯链断掉。
    * 用行号: 规格书改版时行序调整会让键整体漂移, 后果同上。
    限值形态(0~11.1 / 0.1)对同一需求的多行天然不同, 且只在规格书真的改了
    限值时才变 —— 那本来就是"判据变了", 下游重置为 draft 是正确行为。
    """
    rail = (getattr(cond, "rail", "") or "").strip() or "na"
    lim = getattr(cond, "limits", {}) or {}
    tag_parts = []
    for k in ("min", "typ", "max"):
        v = lim.get(k)
        if v is not None:
            tag_parts.append(f"{k[0]}{float(v):g}")
    bound = "".join(tag_parts) or "na"
    base = f"{cond.req_id}@{rail}"
    code = f"{base}#{bound}"
    n = 2
    while code in used:
        code = f"{base}#{bound}_{n}"
        n += 1
    return code


def _qualifier_of(cond: Any) -> str:
    """工况限定词 (长期/短期等) —— 取自工作制子句, 供下游区分同名测点。"""
    for c in getattr(cond, "input_conditions", ()):
        if getattr(c, "kind", "") == "duty":
            return (getattr(c, "text", "") or "")[:64]
    return ""


def bundle_from_result(
    result: Any,
    *,
    generator: str = "aterag-extract/1.0",
    product_doc_fingerprint: str = "",
    removed_requirement_codes: Sequence[str] = (),
    baseline: Mapping[str, Any] | None = None,
) -> BundleModel:
    """ExtractionResult -> BundleModel (导出的唯一入口)。

    baseline: 上次导出的 bundle, 用于计算"本次消失的需求"。不传则不做删除对比
    —— 首次导出没有基线, 此时不应把所有需求都标成 removed。
    """
    scen_by_req: dict[str, list[Any]] = {}
    for s in getattr(result, "scenarios", ()) or ():
        scen_by_req.setdefault(s.req_id, []).append(s)

    assess_by_req: dict[str, dict[str, str]] = {}
    for a in getattr(result, "assessments", ()) or ():
        assess_by_req[a.req_id] = {
            "verdict": a.verdict,
            "rule_id": a.rule_id,
            "basis": a.basis,
        }

    # requirement_code 是下游幂等键, 必须唯一; 而同一规格编号可能有多行
    # (PA601 的 SR-1203 有 -54V长期/-54V短期/3.45V长期三行共用一个编号)。
    # 先统计每个编号的行数: 只有多行编号才追加行限定词 —— 单行编号保持原样,
    # 这样绝大多数需求的下游编号与规格书完全一致, 便于人工核对。
    row_counts: dict[str, int] = {}
    for c in result.conditions:
        row_counts[c.req_id] = row_counts.get(c.req_id, 0) + 1

    reqs: list[RequirementModel] = []
    used_codes: set[str] = set()
    for c in result.conditions:
        m = requirement_to_model(c, scen_by_req.get(c.req_id, ()), assess_by_req.get(c.req_id))
        m.spec_requirement_id = c.req_id
        if row_counts.get(c.req_id, 0) > 1:
            m.requirement_code = _row_code(c, used_codes)
        else:
            # 单行编号理论上不会撞, 但仍防御: 万一抽取侧改坏也不至于
            # 把整批导入变成"幂等键冲突"的全量失败。
            code = c.req_id
            n = 2
            while code in used_codes:
                code = f"{c.req_id}~{n}"
                n += 1
            m.requirement_code = code
        used_codes.add(m.requirement_code)
        reqs.append(m)

    removed: list[str] = []
    if baseline is not None:
        # 基线存的是规格书原编号 —— requirement_code 带行限定词, 判据一变就变,
        # 拿它做消失检测会把"改限值"误判成"删了又加"。
        prev = set()
        for key in ("spec_requirement_ids", "requirements"):
            vals = baseline.get(key) if isinstance(baseline, Mapping) else None
            if vals:
                prev = set(vals)
                break
        cur = {r.spec_requirement_id or r.requirement_code for r in reqs}
        removed = sorted(prev - cur)

    bundle = BundleModel(
        bundle_version=BUNDLE_VERSION,
        contract_hash=contract_hash(),
        product_code=result.model_id,
        doc_version=result.doc_version or "",
        product_doc_fingerprint=product_doc_fingerprint,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        generator=generator,
        bundle_fingerprint="",
        requirements=reqs,
        excluded=[
            ExcludedModel(
                req_id=e.req_id,
                title=e.title,
                section_path=e.section_path,
                reason=e.reason,
                matched_word=getattr(e, "matched_word", "") or "",
                notes=getattr(e, "notes", "") or "",
            )
            for e in result.excluded
        ],
        review=[
            ReviewModel(
                kind=r.kind,
                section_path=r.section_path,
                heading=r.heading,
                detail=r.detail,
                hint=r.hint,
                method_ref=getattr(r, "method_ref", "") or "",
            )
            for r in result.needs_review
        ],
        excluded_scenarios=[
            ExcludedScenarioModel(
                req_id=e.req_id,
                title=e.title,
                bindings=dict(e.bindings),
                reason=e.reason,
                rule_id=getattr(e, "rule_id", "") or "",
            )
            for e in (getattr(result, "excluded_scenarios", ()) or ())
        ],
        stats=dict(result.stats),
        removed_requirement_codes=removed,
    )
    # bundle 指纹在字段齐备后算, 覆盖全部需求指纹 —— 用于下游"这批有没有变"
    bundle.bundle_fingerprint = _fp(*[r.req_fingerprint for r in bundle.requirements])
    return bundle
