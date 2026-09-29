"""产测充分性评估 (A6') —— 补齐之后, 判定这些条件对产测是否"充分且必要"。

问题
----
业界方法把"缺哪侧补哪侧"补到了 86/95 双边齐全, 但补齐本身只保证"有",
不保证"对"。三类误判在补齐后依然存在, 且都只能靠判断发现:

  1. 不充分 (insufficient): 条件齐了但测不了 —— 缺测量点/缺判定方式/
     缺仪器动作, 用例写出来也跑不起来。
  2. 不必要 (unnecessary): 给不可测项硬补了条件 (如给"三防要求"补额定输入) ——
     看着完整, 实则把不可测项伪装成可测项, 反而污染覆盖率口径。
  3. 越界 (out_of_scope): 条件属于研发/安规/环境试验范畴, 不该出现在产测项里。

判定口径
--------
三类判定都由本模块给出"发现 + 依据", 但**不自动改动任何结论**:
发现写入 AssessmentItem 交人工裁定, 与 needs_review 同一处置通道。
理由同补齐层 —— 判定"什么不必测"是取舍决策, 猜错的代价远大于漏判。

不做什么
--------
* 不用 LLM。评估规则全部来自 test_methods.yaml 的 assess 段, 代码零词汇。
* 不删除/不标记任何需求为"不必要"。只报告。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml

from aterag.extract.models import STATUS_APPROVED, ReviewItem, TestCondition

#: 评估结论词表 (封闭 —— 下游按 key 聚合报告)。
VERDICT_SUFFICIENT = "sufficient"
VERDICT_INSUFFICIENT = "insufficient"
VERDICT_UNNECESSARY = "unnecessary"
VERDICT_OUT_OF_SCOPE = "out_of_scope"
#: 条件齐全但含未人审提案 —— 单列一类: 补齐刚发生时全体都会命中,
#: 若并入 insufficient 会把"真的缺一侧"这个真问题淹没。
VERDICT_PENDING_REVIEW = "pending_review"

VERDICTS = (
    VERDICT_SUFFICIENT,
    VERDICT_INSUFFICIENT,
    VERDICT_UNNECESSARY,
    VERDICT_OUT_OF_SCOPE,
    VERDICT_PENDING_REVIEW,
)

#: 需要人工作出取舍决策的结论 (待签字不算 —— 签字是流程动作, 不是取舍)。
VERDICTS_NEEDING_DECISION = (
    VERDICT_INSUFFICIENT,
    VERDICT_UNNECESSARY,
    VERDICT_OUT_OF_SCOPE,
)


@dataclass(frozen=True, slots=True)
class AssessRule:
    """一条评估规则。

    Attributes:
        id: 规则标识
        basis: 判定依据 (标准/通行做法), 供人工复核
        verdict: 产出哪一类结论
        when: 触发条件声明, 支持: role / title_pattern / has_kind /
              missing_side / has_draft / flags_any
    """

    id: str
    basis: str
    verdict: str
    when: Mapping[str, Any] = field(default_factory=dict)
    hint: str = ""


@dataclass(frozen=True, slots=True)
class RuleBook:
    """评估规则集 (声明在 test_methods.yaml 的 assess.assess_rules)。"""

    rules: tuple[AssessRule, ...] = ()

    @classmethod
    def load(cls, path: str) -> RuleBook:
        doc = yaml.safe_load(open(path, encoding="utf-8").read()) or {}
        assess = doc.get("assess") or {}
        return cls(
            rules=tuple(
                AssessRule(
                    id=str(r.get("id", "")),
                    basis=str(r.get("basis", "")),
                    verdict=str(r.get("verdict", "")),
                    when=dict(r.get("when") or {}),
                    hint=str(r.get("hint", "")),
                )
                for r in (assess.get("assess_rules") or [])
            )
        )

    def validate(self) -> None:
        bad: list[str] = []
        for r in self.rules:
            if not r.id:
                bad.append("存在无 id 的评估规则")
            if not r.basis:
                bad.append(f"assess_rules[{r.id}] 缺 basis (判定须有依据)")
            if r.verdict not in VERDICTS:
                bad.append(f"assess_rules[{r.id}].verdict 非法: {r.verdict}")
        if not self.rules:
            bad.append("test_methods.yaml 的 assess.assess_rules 为空 (评估层会静默失效)")
        if bad:
            raise ValueError("产测充分性评估规则不自洽: " + "; ".join(bad))


@dataclass(frozen=True, slots=True)
class AssessmentItem:
    """一条评估发现。

    verdict=sufficient 时 also 表示"该需求已评估通过", 供覆盖率统计。
    """

    req_id: str
    title: str
    section_path: str
    verdict: str
    rule_id: str
    basis: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "req_id": self.req_id,
            "title": self.title,
            "section_path": self.section_path,
            "verdict": self.verdict,
            "rule_id": self.rule_id,
            "basis": self.basis,
            "detail": self.detail,
        }


def _missing_sides(cond: TestCondition) -> list[str]:
    out: list[str] = []
    if not cond.input_conditions:
        out.append("input")
    if not cond.output_conditions:
        out.append("output")
    return out


def _all_kinds(cond: TestCondition) -> set[str]:
    return {c.kind for c in (*cond.input_conditions, *cond.output_conditions)}


def _has_draft(cond: TestCondition) -> bool:
    return any(
        c.status != STATUS_APPROVED for c in (*cond.input_conditions, *cond.output_conditions)
    )


def _match(rule: AssessRule, cond: TestCondition) -> str | None:
    """规则是否命中该需求; 命中则返回说明, 未命中返回 None。"""
    w = rule.when
    if not w:
        return None  # 无触发条件的规则不生效 (避免"配置写一半就全量命中")
    if "role" in w and cond.role != w["role"]:
        return None
    if "title_pattern" in w and not re.search(str(w["title_pattern"]), cond.title or ""):
        return None
    if "missing_side" in w:
        if str(w["missing_side"]) not in _missing_sides(cond):
            return None
    if "missing_any_side" in w and not _missing_sides(cond):
        return None
    if "has_kind" in w:
        want = w["has_kind"]
        kinds = _all_kinds(cond)
        if not (kinds & set(want if isinstance(want, list) else [want])):
            return None
    if "has_draft" in w and _has_draft(cond) != bool(w["has_draft"]):
        return None
    if "flags_any" in w:
        want = w["flags_any"]
        if not set(want if isinstance(want, list) else [want]) & set(cond.flags):
            return None
    if "min_clauses" in w:
        total = len(cond.input_conditions) + len(cond.output_conditions)
        if total < int(w["min_clauses"]):
            return None
    if "section_prefix" in w and not (cond.section_path or "").startswith(str(w["section_prefix"])):
        return None
    return rule.hint or rule.id


def assess_conditions(
    conditions: Sequence[TestCondition],
    book: RuleBook,
) -> list[AssessmentItem]:
    """对每个需求给出充分性判定 (一条需求至多一条结论, 取首个命中的规则)。"""
    out: list[AssessmentItem] = []
    for cond in conditions:
        for rule in book.rules:
            detail = _match(rule, cond)
            if detail is None:
                continue
            out.append(
                AssessmentItem(
                    req_id=cond.req_id,
                    title=cond.title,
                    section_path=cond.section_path,
                    verdict=rule.verdict,
                    rule_id=rule.id,
                    basis=rule.basis,
                    detail=f"{cond.req_id} {cond.title}: {detail}"[:300],
                )
            )
            break  # 首个命中的规则即为结论
    return out


def summarize(items: Sequence[AssessmentItem]) -> dict[str, Any]:
    """按结论聚合 + 区分"需取舍决策"与"仅待签字"。"""
    by_verdict: dict[str, list[str]] = {}
    for it in items:
        by_verdict.setdefault(it.verdict, []).append(it.req_id)
    return {
        "assessed": len(items),
        "by_verdict": {k: sorted(v) for k, v in sorted(by_verdict.items())},
        # 需人工取舍: 真缺口 / 该剔除 / 不属产测
        "needs_decision": sorted(
            it.req_id for it in items if it.verdict in VERDICTS_NEEDING_DECISION
        ),
        # 仅待签字: 条件齐全, 等评审放行
        "pending_signoff": sorted(
            it.req_id for it in items if it.verdict == VERDICT_PENDING_REVIEW
        ),
    }


def to_review_items(items: Sequence[AssessmentItem]) -> list[ReviewItem]:
    """需人工处理的发现转成待审项, 走既有评审通道。

    sufficient 不入队 (它不是问题); pending_review 也入队 —— 未签字的条件
    按红线本就不能生效, 必须留在可见队列里等签字, 不能因"只差签字"而消失。
    """
    return [
        ReviewItem(
            kind=f"assess:{it.verdict}",
            section_path=it.section_path,
            heading=it.title,
            detail=it.detail,
            hint=f"{it.rule_id}: {it.basis[:90]}",
            method_ref=it.rule_id,
        )
        for it in items
        if it.verdict != VERDICT_SUFFICIENT
    ]
