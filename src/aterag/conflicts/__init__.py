"""冲突消解: Semantica conflicts 模块的 ATERag 桥。

性格: **credibility 之外的排序依据全部拒绝**(红线)。上游 Semantica
rought 四处 fail-open(默认 voting / 未登记 0.5 / confidence 缺 0.5 /
顶格 1.0), 在 :mod:`aterag.conflicts.adapter` 里逐处收口。
"""

from aterag.conflicts.adapter import (
    ONLY_STRATEGY,
    CredibilityOnlyConflictResolver,
    UnknownAuthorityKind,
    make_conflict,
)
from aterag.conflicts.gate import Adjudication, adjudicate

__all__ = [
    "ONLY_STRATEGY",
    "Adjudication",
    "CredibilityOnlyConflictResolver",
    "UnknownAuthorityKind",
    "adjudicate",
    "make_conflict",
]
