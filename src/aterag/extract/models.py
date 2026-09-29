"""抽取结果的数据契约 (无 I/O, 无外部依赖).

三条可见性原则贯穿本包:
  1. 三桶并存 —— conditions / excluded / needs_review 全部可审计, 不静默丢弃。
  2. 溯源必带 —— 每条条件都带 section_path + req_id + 原文片段, 便于人工复核。
  3. 置信度显式 —— rule / annotated / proposed 三级, "未人审" 绝不下游无痕。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 置信度: rule=规则命中, annotated=人审注记, proposed=LLM 提案(未人审)
CONF_RULE = "rule"
CONF_ANNOTATED = "annotated"
CONF_PROPOSED = "proposed"

# 条件来源: notes / signal_req / limits / annotation / title / industry_method
SRC_NOTES = "notes"
SRC_SIGNAL_REQ = "signal_req"
SRC_LIMITS = "limits"
SRC_TITLE = "title"
SRC_ANNOTATION = "annotation"
# 业界方法补齐 (test_methods.yaml): 规格书没写测试条件时按标准/通行做法补。
# 与 SRC_NOTES 等"规格书原文"来源严格区分 —— 前者是人读不到规格书里的话,
# 后者在规格书里有出处, 追溯口径不可混同。
SRC_METHOD = "industry_method"

# 子句状态: approved=已人审可生效; draft=提案/注记草稿, 未人审不得进入导出。
# 与 AnnotationEntry.status 同构, 但作用在"单个子句"而非"整条注记"上 ——
# 一条需求的不同子句可以有不同状态 (如规则侧已定、补齐侧待审)。
STATUS_APPROVED = "approved"
STATUS_DRAFT = "draft"

# 角色 (章节先验给出的默认语义)
ROLE_STIMULUS_RESPONSE = "stimulus_response"
ROLE_INPUT_DOMAIN = "input_domain"
ROLE_OUTPUT_SPEC = "output_spec"
ROLE_PROTECTION = "protection_response"
ROLE_SIGNAL_IO = "signal_io"
ROLE_OTHER = "other"

# 条目来源
SRC_ENTITY = "entity"
SRC_BLOCK = "block"


@dataclass
class ConditionClause:
    """一条条件子句: 产品依赖的外部状态, 或产品自身的输出状态。"""

    kind: str  # condition_kinds 词表 key
    text: str  # 原文片段 (溯源用, 不改写)
    role: str  # input | output
    value: dict[str, Any] | None = None  # 结构化值 {"op": ">=", "value": -25, "unit": "℃"}
    source: str = SRC_NOTES
    confidence: str = CONF_RULE
    # 人审状态: approved 可进入导出口; draft 未生效。业界补齐/注记草稿一律 draft,
    # 规则与已签注记为 approved —— 默认 approved 使"有出处即可用"保持不变。
    status: str = STATUS_APPROVED
    # 补齐该子句的方法 id (test_methods.yaml 的 methods[].id), 空=非补齐产物。
    # 溯源用: 让"这条常识前提出自哪条标准"可查, 评审时可核对依据。
    method_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TestCondition:
    """一条产测条件: 激励 -> 响应。"""

    req_id: str
    title: str
    section_path: str
    heading: str = ""
    priority: str = ""
    rail: str = ""
    unit: str = ""
    # 备注原文: 条件装配的输入源之一, 且常含限值之外的适用条件
    # (如 SR-1204 的 "90~176Vac: 400W; 176~286Vac: 600W" 输入分档)。
    # 不带出原文就无法判断"某限值在什么条件下成立", 属于溯源缺失。
    notes: str = ""
    # 输入/输出条件合成后的可读描述 (由 supplement.render_descriptions 写入,
    # 模板声明在 test_methods.yaml)。下游产测直接呈现这段, 免得再各自拼一遍。
    description: str = ""
    role: str = ROLE_STIMULUS_RESPONSE
    input_conditions: list[ConditionClause] = field(default_factory=list)
    output_conditions: list[ConditionClause] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    etype: str = "Requirement"
    source: str = SRC_ENTITY

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["input_conditions"] = [c.to_dict() for c in self.input_conditions]
        d["output_conditions"] = [c.to_dict() for c in self.output_conditions]
        return d


@dataclass
class ExcludedItem:
    """被剔除的条目 —— 必须带原因, 剔除本身也要可审计。"""

    req_id: str
    title: str
    section_path: str
    priority: str = ""
    reason: str = ""
    matched_word: str = ""
    field: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewItem:
    """待人工归档项: 落在选择范围内, 但当前规则/档案无法结构化。

    典型来源: 未映射的表 (T2 缺口) 、无任何规则命中的自由文本。
    """

    kind: str  # unmapped_table | unresolved_text | needs_manual_digitization | ...
    section_path: str
    heading: str
    detail: str
    hint: str = ""  # T1 推断的角色等提示
    rows: int = 0
    # 补齐方法 id (test_methods.yaml), 让"为何待审"可回溯到具体方法条目。
    method_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Selection:
    """章节选择结果。"""

    keywords: list[str]
    matched_headings: list[str]
    section_prefixes: list[str]
    selected_chunk_ids: list[str]
    blocks_total: int
    blocks_selected: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractionResult:
    model_id: str
    doc_version: str = ""
    profile: str = ""
    source: str = SRC_ENTITY  # 实体来源: blocks(离线确定性) | postgres(RAG 实存)
    selection: Selection | None = None
    conditions: list[TestCondition] = field(default_factory=list)
    excluded: list[ExcludedItem] = field(default_factory=list)
    needs_review: list[ReviewItem] = field(default_factory=list)
    # 已被人工评审判定为"不提取"的条目 (附理由) —— 与 needs_review 互斥
    reviewed_dispositions: list[ReviewItem] = field(default_factory=list)
    # 产测充分性评估结论 (A6'): 每条需求一条, 含 sufficient/pending_review/
    # insufficient/unnecessary/out_of_scope 与判定依据。
    assessments: list[Any] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "doc_version": self.doc_version,
            "profile": self.profile,
            "source": self.source,
            "selection": self.selection.to_dict() if self.selection else None,
            "conditions": [c.to_dict() for c in self.conditions],
            "excluded": [e.to_dict() for e in self.excluded],
            "needs_review": [r.to_dict() for r in self.needs_review],
            "reviewed_dispositions": [r.to_dict() for r in self.reviewed_dispositions],
            "assessments": [a.to_dict() if hasattr(a, "to_dict") else a for a in self.assessments],
            "stats": self.stats,
        }


class SectionKeywordNotFound(LookupError):
    """章节关键字未命中任何章节 —— fail-closed。

    区别于"命中章节但条目全被剔除"(合法空结果): 前者是配置错误或文档结构变化,
    返回空列表会让上层误以为"该章节没有产测条件"。
    """


class ModelNotIngested(LookupError):
    """型号未入库 (无 blocks 侧车) —— fail-closed, 不用空结果冒充。"""
