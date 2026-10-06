"""决策谱系 -> 结构化推理路径(读侧投影)。

**为什么用上游的数据模型而不是上游的散文生成器**
----------------------------------------------------
:mod:`semantica.reasoning.explanation_generator` 里
:meth:`ExplanationGenerator.generate_explanation` 的自然语言段是**英文
模板**: ``Based on N premises, we conclude: ...`` —— 它把前提当字符串
数组渲染, 把我们上一片刚接上的 ``input_sources``(每个输入的 SR 出处)
和出处可信度**全部丢掉**。用它换来的是一段读不懂审计链的英文, 丢掉的是
「这个数值是从哪条 SR 来的」—— 那正是产测追责要的半条链。

所以本模块**复用上游的数据模型**(:class:`Explanation` /
:class:`ReasoningPath` / :class:`ReasoningStep` / :class:`Rule` 都是
Semantica 的 dataclass, 与上游其它推理件的形状一致, 将来换消费方不用改
数据), 渲染自己写中文审计文本。判定依据: 复用的是**结构**, 不用的是
**丢了审计链的排版**。

**不顶格**
----------------------------------------------------
上游 :class:`ReasoningStep` / :class:`ReasoningPath` 的 ``confidence``
默认 1.0。与之前 ``track_entity`` 缺省顶格是同一类病: 未标注的出处可信
度在结构化字段里显示成「已验证」。这里显式下发: 标注了就用标注值, 没标
注落 0.0 并在 ``metadata['credibility_unknown']`` 记明「未标注」——
0.0 至少不会被误读成已验证, 且与「确实标了 0」可区分。
"""

from __future__ import annotations

from typing import Any

from semantica.reasoning.explanation_generator import (
    Explanation,
    ReasoningPath,
    ReasoningStep,
)
from semantica.reasoning.reasoner import Rule

#: 来源不明的输入在文本里的显式说法。与 :mod:`aterag.inference.engine`
#: ``_input_sources`` 写的 ``(caller)`` 同义 —— 调用方给的, 不在型号事实里。
CALLER_SOURCE = "(caller)"

#: 出处可信度未标注时的文字说法。
UNKNOWN_CREDIBILITY = "未标注"


def _credibility_of(entry: dict[str, Any]) -> float | None:
    """谱系行里的出处可信度。

    取 ``metadata['credibility']``(决策记录写的位置), 退化到行级
    ``credibility`` 字段(上游 ProvenanceEntry 有这个顶层字段; 我们这层
    存的是 JSONB 里的 metadata, 顶层多半是 None)。两处都没有 -> None,
    由调用方落「未标注」。
    """
    meta = entry.get("metadata") or {}
    val = meta.get("credibility")
    if val is None:
        val = entry.get("credibility")
    return float(val) if val is not None else None


def _input_lines(entry: dict[str, Any]) -> list[str]:
    """每个输入一行: ``名称 = 数值 <- 出处``。

    输入与出处的对应来自 ``metadata['inputs']`` / ``metadata['input_sources']``,
    两者键集可能不齐(旧记录没有 input_sources), 缺出处的一律显式写成
    ``(未标注)`` —— 不留白让人以为「就是本系统算的」。
    """
    meta = entry.get("metadata") or {}
    inputs: dict[str, Any] = meta.get("inputs") or {}
    sources: dict[str, Any] = meta.get("input_sources") or {}
    keys = list(inputs) + [k for k in sources if k not in inputs]
    lines: list[str] = []
    for name in keys:
        raw = inputs.get(name)
        value = "(未提供)" if raw is None else str(raw)
        src = sources.get(name)
        if isinstance(src, dict):
            loc = src.get("req_id") or src.get("section_path") or "(未标注)"
        else:
            loc = str(src) if src else "(未标注)"
        lines.append(f"{name} = {value} <- {loc}")
    return lines


def explain(entry: dict[str, Any]) -> tuple[Explanation, str]:
    """谱系行 -> (上游 Explanation 结构, 中文审计文本)。

    同一条记录两次调用产出同样的内容: ``explanation_id`` 只用
    ``entity_id``, 不掺时间戳 —— 解释文本要能被 diff 与断言。
    """
    meta = entry.get("metadata") or {}
    entity_id = str(entry.get("entity_id") or "unknown")
    rule_id = str(meta.get("rule_id") or "(未标注规则)")
    statement = str(meta.get("statement") or rule_id)
    output = str(meta.get("output") or "result")
    value = str(meta.get("value") if meta.get("value") is not None else "(无)")
    cred = _credibility_of(entry)
    unknown = cred is None
    cred_value = 0.0 if unknown else cred

    premises = _input_lines(entry)
    conclusion = f"{output} = {value}"

    rule = Rule(
        rule_id=rule_id,
        name=statement,
        conditions=premises,
        conclusion=conclusion,
        confidence=cred_value,
        metadata={"credibility_unknown": unknown},
    )
    steps = [
        ReasoningStep(
            step_id=f"premise_{i}",
            description=line,
            input_facts=[line],
            metadata={"kind": "premise"},
        )
        for i, line in enumerate(premises)
    ]
    steps.append(
        ReasoningStep(
            step_id="rule_step",
            description=f"应用规则 {rule_id}: {statement}",
            rule_applied=rule,
            input_facts=list(premises),
            output_fact=conclusion,
            confidence=cred_value,
            metadata={"credibility_unknown": unknown},
        )
    )
    path = ReasoningPath(
        path_id=f"path_{entity_id}",
        steps=steps,
        start_facts=list(premises),
        end_conclusion=conclusion,
        total_confidence=cred_value,
        metadata={
            "credibility_unknown": unknown,
            "source_document": entry.get("source_document"),
            "checksum": entry.get("checksum"),
            "previous_checksum": entry.get("previous_checksum"),
        },
    )
    explanation = Explanation(
        explanation_id=f"exp_{entity_id}",
        explanation_type="inference",
        conclusion=conclusion,
        reasoning_path=path,
        metadata={
            "rule_id": rule_id,
            "decision_id": entity_id.split("dec:", 1)[-1],
            "credibility_unknown": unknown,
        },
    )
    return explanation, render_audit(entry, explanation)


def render_audit(entry: dict[str, Any], explanation: Explanation) -> str:
    """中文审计文本。**这是给人看的那一份**, 出处链与可信度必须齐全。"""
    meta = entry.get("metadata") or {}
    lines = [
        f"决策 {explanation.metadata['decision_id']}",
        f"规则 {explanation.metadata['rule_id']}"
        + (f"  {meta.get('statement')}" if meta.get("statement") else ""),
        "输入:",
    ]
    for step in explanation.reasoning_path.steps:
        if step.metadata.get("kind") == "premise":
            lines.append(f"  {step.description}")
    lines.append(f"结论 {explanation.conclusion}")
    cred = _credibility_of(entry)
    lines.append(f"出处可信度 {UNKNOWN_CREDIBILITY if cred is None else cred}")
    src_doc = entry.get("source_document")
    if src_doc:
        lines.append(f"规则出处 {src_doc}")
    if meta.get("domain_layer"):
        lines.append(f"领域层 {meta['domain_layer']}")
    return "\n".join(lines)


__all__ = ["CALLER_SOURCE", "UNKNOWN_CREDIBILITY", "explain", "render_audit"]
