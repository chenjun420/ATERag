"""冲突消解 Semantica 桥: 性格是「credibility 之外的排序依据全部拒绝」。

**为什么要包一层而不是直接用**
--------------------------------
Semantica 的 :class:`~semantica.conflicts.conflict_resolver.ConflictResolver`
有四处 fail-open, 逐处实测(上游 0.7.0):

===========  ================================================  ==========
上游行为     位置                                              ATERag 红线
===========  ================================================  ==========
默认策略     ``config.get("default_strategy", "voting")``      只用 credibility
来源缺登记   ``get_source_credibility`` 未注册 -> ``0.5``      分值必须显式分档
confidence   ``conflict.sources[i].get("confidence", 0.5)``    不猜
             缺失 -> ``0.5``
confidence   ``ConflictDetector`` 里                           顶格 = 凭空
             ``entity.get("confidence", 1.0)`` -> 顶格         发最高可信
===========  ================================================  ==========

四条里任何一条放进来, 消解结果都像是「有依据」: 0.5 的来源能赢 0.2 的、
多数票能赢权威条款。所以本适配层**收口全部入口**:

- 策略只允许 :attr:`ResolutionStrategy.CREDIBILITY_WEIGHTED`(以及不可自动
  消解时的 ``manual_review`` 出口), 调用方给别的策略名直接 ``ValueError``,
  不做「黑名单放过其余」—— 红线是白名单不是黑名单。
- 参与排序的每个来源文档必须先 :meth:`register_source` 登记过, 且
  分值**只能**来自 :data:`aterag.provenance.seed_loader.CREDIBILITY_BY_AUTHORITY`
  (与种子谱系同一量纲、同一出处, 不另发一套分数 —— 第二套分级就是双源)。
  未登记来源的冲突不消解, 转 manual_review 并给出漏掉谁。
- 来源记录的 ``confidence`` 缺失(=「出处未标可信度」)同样走 manual_review,
  绝不落 ``0.5`` 缺省。
"""

from __future__ import annotations

from typing import Any

from semantica.conflicts.conflict_detector import Conflict, ConflictType, SourceTracker
from semantica.conflicts.conflict_resolver import (
    ConflictResolver,
    ResolutionResult,
    ResolutionStrategy,
)

from aterag.provenance.seed_loader import CREDIBILITY_BY_AUTHORITY

#: 红线: 冲突消解**只用** credibility。多州票数(recency/first_seen/highest
#: confidence)诸类排序依据全部在白名单外 —— 要的不是「别写错」, 是
#: 「调用方连写出这个选项的途径都没有」。
ONLY_STRATEGY = ResolutionStrategy.CREDIBILITY_WEIGHTED


class UnknownAuthorityKind(ValueError):
    """登记的 ``authority_kind`` 不在分级表里。

    **不猜分值**是这个层的地基: 一个没分过档的出处拿未知可信度去参与
    消解, 输出的胜者连「为什么赢」都答不上来。报错让登记方补表(改
    ``CREDIBILITY_BY_AUTHORITY``, 一处, 全库同量纲), 而不是铸造私下分值。
    """


class CredibilityOnlyConflictResolver:
    """包装 :class:`ConflictResolver`, 收口成 credibility-only 入口。"""

    def __init__(self) -> None:
        self._tracker = SourceTracker()
        # upstream 缺省是 "voting" —— 必须显式改掉, 让「不传策略」的调用
        # 也落在 credibility 上(而不是靠每个调用点记得传)。
        self._resolver = ConflictResolver(default_strategy=ONLY_STRATEGY.value)
        self._resolver.set_source_tracker(self._tracker)

    # ---------------------------------------------------------------- 登记

    def register_source(self, document: str, *, authority_kind: str) -> float:
        """按 ``authority_kind`` 从分级表取 credibility, 登记该文档。

        表里没有的 kind **抛错**而不是缺省 —— 分值是放行消解的钥匙,
        钥匙不能由调用方随手造。
        """
        kind = str(authority_kind)
        cred = CREDIBILITY_BY_AUTHORITY.get(kind)
        if cred is None:
            raise UnknownAuthorityKind(
                f"authority_kind={kind!r} 不在 CREDIBILITY_BY_AUTHORITY 分级表里, "
                f"现有: {sorted(CREDIBILITY_BY_AUTHORITY)}。"
                f"先补表(与种子谱系同量纲), 不要在本层猜分值"
            )
        self._tracker.register_source(str(document), kind, credibility_score=cred)
        return cred

    def credibility_of(self, document: str) -> float | None:
        """已登记文档的分值; 未登记返回 None —— 不学上游返回 0.5。"""
        return self._tracker.source_credibility.get(str(document))

    # ---------------------------------------------------------------- 消解

    def resolve(
        self, conflict: Conflict, *, strategy: str | ResolutionStrategy | None = None
    ) -> ResolutionResult:
        """消解一条冲突。红线在三个层次上钉死:

        1. ``strategy`` 给了非 None 就抛 —— 换策略的入口不存在;
        2. 任何来源未登记 / confidence 缺失 -> 不进上游(上游会拿
           0.5 缺省参与加权), 转 manual_review;
        3. **加权平局 -> 转 manual_review**: 上游取
           ``max(value_weights.items(), key=weight)``, 权重相等时 Python
           的 ``max`` 返回**先遇到**的那个 —— 也就是「哪条记录排在前面
           哪个赢」。同档来源(比如刚定为同级的国标与厂商规格书)相遇时
           这就是纯遍历顺序决定胜负, 换句话说换一次输入顺序结论就变。
           平局没有 credibility 依据可依, 所以转人审。
        """
        if strategy is not None and strategy != ONLY_STRATEGY:
            raise ValueError(f"冲突消解只用 credibility(红线): 不接受 strategy={strategy!r}")
        missing = [
            str(s.get("document", "unknown"))
            for s in conflict.sources
            if self.credibility_of(str(s.get("document", "unknown"))) is None
        ]
        if missing:
            return self._manual_review(conflict, f"来源未登记 credibility: {missing}")
        no_conf = [i for i, s in enumerate(conflict.sources) if s.get("confidence") is None]
        if no_conf:
            return self._manual_review(
                conflict,
                f"来源记录缺 confidence(出处可信度未标注): 索引 {no_conf}",
            )
        result = self._resolver.resolve_conflict(conflict)  # -> CREDIBILITY_WEIGHTED
        if not result.resolved:
            return result
        # 平局探测: **行为探测而不是复制加权公式**。上游不暴露权重表,
        # 自己按 ``confidence * credibility`` 重算一遍等于把上游的公式抄
        # 第二份(上游一改就静默错)。改用「顺序敏感性」判据: 把
        # 值与来源整体倒序再解一次, 结果变了就说明结论依赖顺序 ->
        # 平局。上游公式怎么变这个判据都成立。
        flipped = self._resolver.resolve_conflict(_reversed(conflict))
        if flipped.resolved and _differs(result.resolved_value, flipped.resolved_value):
            return self._manual_review(
                conflict,
                "credibility 加权平局: 倒序重解得到不同胜者, "
                "说明胜负取决于来源顺序而非出处可信度, 转人审",
            )
        return result

    @staticmethod
    def _manual_review(conflict: Conflict, note: str) -> ResolutionResult:
        """「消解不了, 转人审」是一条**结果**而不是失败: 带着原因出去,
        让审查者知道要补的是登记表还是来源标注, 而不是猜一个赢者。"""
        return ResolutionResult(
            conflict_id=conflict.conflict_id,
            resolved=False,
            resolved_value=None,
            sources_used=[str(s.get("document", "unknown")) for s in conflict.sources],
            resolution_notes=note,
        )


def _reversed(conflict: Conflict) -> Conflict:
    """值与来源同步倒序的新 Conflict。

    ``conflicting_values[i]`` 与 ``sources[i]`` 是配对的, 所以必须**同步**
    倒序 —— 只倒一个会让人拿 A 的出处去称 B 的值, 那是伪造证据, 比平局
    未被发现更糟。
    """
    return Conflict(
        conflict_id=conflict.conflict_id,
        conflict_type=conflict.conflict_type,
        entity_id=conflict.entity_id,
        property_name=conflict.property_name,
        conflicting_values=list(reversed(conflict.conflicting_values)),
        sources=list(reversed(conflict.sources)),
        confidence=conflict.confidence,
        severity=conflict.severity,
        recommended_action=conflict.recommended_action,
    )


def _differs(a: Any, b: Any) -> bool:
    """两个胜者是不是不同。用 ``str()`` 比而不是 ``!=``: 上游以
    ``_hashable_key`` 归并值, 5 与 5.0 会被它当同一个键; 我们这里只判
    「有没有换人」, 字符串形态不同就当换了 —— 宁可多转一次人审, 不可
    漏掉一次顺序依赖。"""
    return str(a) != str(b)


def make_conflict(
    entity_id: str,
    property_name: str,
    values: list[Any],
    sources: list[dict[str, Any]],
) -> Conflict:
    """按上游 :class:`Conflict` 形造一条 VALUE_CONFLICT。

    直接构造而不是绕道 ``ConflictDetector.detect_value_conflicts``: 那边
    按 ``entity.get('id')`` 分组并把 ``entity.get('confidence', 1.0)`` 顶格,
    两条都不适配本层的 fail-closed 输入; 分组在 :func:`adjudicate` 里
    自己做, ``Conflict`` 只当数据结构用。
    """
    return Conflict(
        conflict_id=f"{entity_id}_{property_name}_conflict",
        conflict_type=ConflictType.VALUE_CONFLICT,
        entity_id=entity_id,
        property_name=property_name,
        conflicting_values=list(values),
        sources=[dict(s) for s in sources],
        # 冲突自身的置信度不是来源可信度: 这里置顶格会让审查者误读
        # 成「冲突判定很确定」; 胜负该由 resolver 的结果表达。
        confidence=1.0,
        severity="high" if len({str(v) for v in values}) > 1 else "medium",
        recommended_action="credibility_weighted 或人工审查",
    )


__all__ = [
    "ONLY_STRATEGY",
    "CredibilityOnlyConflictResolver",
    "UnknownAuthorityKind",
    "make_conflict",
]
