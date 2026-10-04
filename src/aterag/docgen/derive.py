"""``derive_from`` 的补全策略(D2)。

§18.9 G4(合并阻断)要求 ``formula.derive_from`` 非空, 或标注「实验定律」。
实测:129 条量纲闭合公式里 **65 条**该列为空, 且方案中**无一条**标了
「实验定律」。

## 为什么不能直接填个值

``derive_from`` 记的是公式的公理/定理出处(``A-2``/``A-4``/``T3``)。它虽然
**不参与**量纲计算, 但它是审计与「四遥」追溯的入口 —— 填错不会让任何测试变红,
只会让人拿着一条错误的溯源去核安全边界。所以本模块的硬规则是:

**每一次补全都必须带来源标记, 且「继承」与「声明」在数据上可区分。**

三类来源:

``explicit``
    方案表格里本来就写了。**不改动**, 原样透传。
``inherited:<section>``
    本章内有兄弟公式写了出处, 且**兄弟之间取值完全一致**, 故按章继承。
    章节是推导单位 —— 实测 ``F_J.2`` 全章统一为 ``(A-2, A-4, T3)``,
    ``F_J.5`` 全章统一为 ``(A-10, T12)``, 章内无分歧。
``empirical-pending-review``
    本章内**没有任何**兄弟写了出处。按 D2 的决定标为「实验定律(待复核)」,
    并置 :attr:`Derivation.review_required` —— 这批是**已知缺口**, 不是已解决。

## 为什么继承要求「兄弟取值完全一致」

若同章兄弟对同一条公式给出**不同**出处, 说明方案在该章内自相矛盾, 此时
任何选取都是猜测。这种情况不进继承, 直接落到 ``empirical-pending-review``,
让缺口显式化。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .spec_parse import FormulaRow

__all__ = [
    "Derivation",
    "EMPIRICAL_PENDING",
    "derive_all",
    "section_of",
]

#: 标注「实验定律」时的固定文本。带「待复核」后缀是刻意的: 这批公式并未
#: 真的被核实为实验定律(例如 ``F_J.8.1_SHOOT_THROUGH`` 是电路分析),
#: 写纯「实验定律」会让人误以为已核实。
EMPIRICAL_PENDING = "实验定律(待复核)"

#: 章 = 域字母 + 第一个数字段: ``F_J.2.4_CUK`` -> ``F_J.2``。
#: 章节是方案的推导单位(附录 J.2 整章同源), 故继承以章为界。
_SECTION_RE = re.compile(r"(F_[A-Z]\.\d+)")


def section_of(formula_id: str) -> str:
    """取公式所属章; 不匹配时返回原 ID(退化为「只跟自己一组」)。"""
    match = _SECTION_RE.match(formula_id)
    return match.group(1) if match else formula_id


@dataclass(frozen=True)
class Derivation:
    """一条公式的 ``derive_from`` 及其来源。

    ``provenance`` 是**审计字段**, 不参与量纲计算, 但决定了
    ``review_required`` —— 上游必须能区分「方案写的」与「我们补的」。
    """

    formula_id: str
    refs: tuple[str, ...]
    provenance: str
    review_required: bool

    @property
    def is_inherited(self) -> bool:
        return self.provenance.startswith("inherited:")

    @property
    def is_explicit(self) -> bool:
        return self.provenance == "explicit"


def derive_all(rows: dict[str, FormulaRow]) -> dict[str, Derivation]:
    """为每个公式 ID 算出 ``derive_from`` 与来源标记。

    兄弟查找用**全部**公式行(不只是量纲闭合的那些): ``upstream`` 是元数据,
    与量纲闭合无关, 一个未闭合的兄弟照样能提供有效的出处信息。
    """
    by_section: dict[str, set[tuple[str, ...]]] = {}
    for fid, row in rows.items():
        if row.upstream:
            by_section.setdefault(section_of(fid), set()).add(tuple(row.upstream))

    out: dict[str, Derivation] = {}
    for fid, row in rows.items():
        if row.upstream:
            out[fid] = Derivation(fid, tuple(row.upstream), "explicit", False)
            continue
        section = section_of(fid)
        candidates = by_section.get(section, set())
        # 章内取值唯一才继承; 多于一种说明方案自相矛盾, 不猜。
        if len(candidates) == 1:
            only = next(iter(candidates))
            out[fid] = Derivation(fid, only, f"inherited:{section}", False)
        else:
            out[fid] = Derivation(fid, (EMPIRICAL_PENDING,), "empirical-pending-review", True)
    return out


def summarize(derivations: dict[str, Derivation]) -> Counter[str]:
    """按来源类别计数, 供门禁输出。"""
    return Counter(d.provenance.split(":", 1)[0] for d in derivations.values())
