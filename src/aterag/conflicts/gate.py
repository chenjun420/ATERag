"""裁决门: 同一实体的多条来源记录 -> 消解或转人审, 不静默双存。

**它在系统的哪个位置**
--------------------------------
现实的冲突面是「**同一实体 id, 多条不同来源的记录**」落在摄取管线里:

1. 现在的管线只从单一规格书抽型号知识, ``save_entities`` 是删后全量插,
   不会出现双源 —— 门在当下只卫一个消费者: **未来接入的第二来源**
   (新版规格书增量、产测数据回填、人工纠正)合并进 ``aterag_entities``
   时的入口;
2. 种子侧重载(改了某条知识的值)走 ``l0_term.provenance`` 的**替换**
   语义, 旧值进了档案行 —— 门不该拦档案(历史就是历史), 只拦「两条
   都自称现行」。

门就是那道红线「不接受副本漂移/双源」的可执行形态: 两值打架时不猜
胜者、不按到达顺序、不按时间戳 —— 只按 credibility(出处分档), 分档
存疑就交人审。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from aterag.conflicts.adapter import (
    CredibilityOnlyConflictResolver,
    make_conflict,
)

#: 裁决出口。
#: - ``single``    只有一条来源, 无从冲突, 直接接受
#: - ``unanimous`` 多条来源同值 —— 互证收敛, 接受
#: - ``resolved``  值打架, credibility 加权分出胜者
#: - ``review``    消解不了(来源未登记/未标可信度), 等人
OUTCOMES = ("single", "unanimous", "resolved", "review")


@dataclass
class Adjudication:
    """一次裁决的完整记录。**为什么存全部来源而不只存胜者**: 复查时要
    能回答「另一个值为什么输了」, 只存胜者的裁决是第二次冲突审判。"""

    entity_id: str
    property_name: str
    outcome: str
    value: Any | None
    sources: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "property_name": self.property_name,
            "outcome": self.outcome,
            "value": self.value,
            "sources": self.sources,
            "notes": self.notes,
        }


def _values_equal(a: Any, b: Any) -> bool:
    """浮点值在「同一物理量」意义上比较: 相对差超过 1e-9 才算不同。

    用近等而不是 ``==``: 不同摄取轮对同一数值会有 599.4 vs 599.4000001
    这类浮点噪声, 拿 ``==`` 会让互证收敛被判成冲突 —— 那是误报。
    阈值取 1e-9**相对**差而不是绝对差, 这样 1e-15 级别的量也不误报。
    非数值退回字符串严格相等(文本出处没有噪声一说)。
    """
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isinf(float(a)) or math.isinf(float(b)):
            return float(a) == float(b)
        na, nb = float(a), float(b)
        scale = max(abs(na), abs(nb), 1e-12)
        return abs(na - nb) / scale <= 1e-9
    return a == b


def adjudicate(
    entity_id: str,
    property_name: str,
    records: list[dict[str, Any]],
    resolver: CredibilityOnlyConflictResolver | None = None,
) -> Adjudication:
    """对同一实体的多条来源记录裁决唯一值。

    ``records`` 形状::

        [{"value": 54.0, "source_document": "spec:PA601-D54A",
          "confidence": 0.95, "authority_kind": "standard"}, ...]

    - ``authority_kind`` 在门这里**必须显式给**: 登记分档没有缺省
      (adapter 会抛), 是故意的 —— 「哪条来源更可信」是数据自己的主张,
      门只负责执行, 不负责发明。
    - ``confidence`` 同理必须给: 上游把缺失当 0.5 / 顶格 1.0 的两条
      fail-open 在 adapter 里已关闭, 到这里的记录少了字段就是
      ``review`` 出口。
    """
    if resolver is None:
        resolver = CredibilityOnlyConflictResolver()

    if len(records) == 1:
        r = records[0]
        return Adjudication(
            entity_id=entity_id,
            property_name=property_name,
            outcome="single",
            value=r.get("value"),
            sources=[dict(r)],
        )

    # 互证收敛: 全部值近等 -> 不消解, 直接接受
    vals = [r.get("value") for r in records]
    if all(_values_equal(v, vals[0]) for v in vals[1:]):
        return Adjudication(
            entity_id=entity_id,
            property_name=property_name,
            outcome="unanimous",
            value=vals[0],
            sources=[dict(r) for r in records],
        )

    # 值打架: 登记全部来源(缺 kind 就炸), 再消解
    for r in records:
        resolver.register_source(
            str(r.get("source_document", "unknown")),
            authority_kind=r["authority_kind"],
        )
    conflict = make_conflict(
        entity_id,
        property_name,
        vals,
        [
            {
                "document": str(r.get("source_document", "unknown")),
                "confidence": r.get("confidence"),
                "section": r.get("section"),
            }
            for r in records
        ],
    )
    result = resolver.resolve(conflict)
    if result.resolved:
        return Adjudication(
            entity_id=entity_id,
            property_name=property_name,
            outcome="resolved",
            value=result.resolved_value,
            sources=[dict(r) for r in records],
            notes=str(result.resolution_notes),
        )
    return Adjudication(
        entity_id=entity_id,
        property_name=property_name,
        outcome="review",
        value=None,
        sources=[dict(r) for r in records],
        notes=str(result.resolution_notes),
    )


__all__ = ["Adjudication", "OUTCOMES", "adjudicate"]
