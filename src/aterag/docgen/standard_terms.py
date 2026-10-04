"""标准术语表:英文术语/缩写 -> 中文名,带出处与优先级。

`name_zh` 的第二来源。方案的公式表只对部分公式给了中文名(实测 130 条闭合
公式里 43 条缺),缺的那批**用标准术语补**,而不是留英文或由我编译名。

## 为什么标准名可以引用, 而 websearch 的译名不行

标准术语是**有出处的**: 说「MTBF 是平均失效间隔时间」, 可以指到
``GB/T 3187-1994《可靠性、维修性术语》`` 的具体条目。这与「我搜到一个看着
合适的译名就填上」有本质区别 —— 后者没有出处, 且漂移不可检测。

## 优先级: GB 国标优先

用户明确要求「GB 国标优先」。排序:

1. ``GB`` / ``GB/T`` —— **国家标准**, 国内认证与招投标的引用依据
2. ``DL/T`` / ``DL`` —— 电力行业标准
3. ``IEC`` —— 国际标准; 本项目 registry 的主体
4. 其它(``UL`` / ``JEDEC`` / ``MIL`` / ``ANSI`` ...) —— 厂商或行业惯例

同一术语有多个来源时取**优先级最高**的, 其余仍记在 :attr:`Term.alternates`
里 —— 不因为选了 GB 就把 IEC 的说法丢掉, 那是信息。

## 已知的译法冲突(必须靠出处消解, 不能猜)

``MTTF`` 在 ``GB/T 3187-1994`` 是「平均失效前时间」, 而 ``DL/T 861-2004``
写作「平均无故障工作时间」。这不是笔误, 是两个体系的不同约定。所以本表**按
(术语, 标准) 存**, 由 :func:`resolve` 按优先级挑, 而不是建一个
``英文 -> 中文`` 的全局字典 —— 后者会在两个标准撞车时静默取一个。

## 未收录即不猜

:func:`resolve` 找不到就返回 ``None``, 调用方**必须**回落到方案自己的名字或
英文短名, 不许自行翻译。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["STANDARD_TERMS", "Term", "priority_of", "resolve"]

#: 标准族优先级, 数字小者优先。GB 国标最高 —— 用户明确要求。
_FAMILY_PRIORITY: tuple[str, ...] = ("GB", "DL", "IEC", "UL", "JEDEC", "MIL", "ANSI")


def priority_of(standard_id: str) -> int:
    """标准族优先级; 未知族排到最后。"""
    head = standard_id.strip().upper().split("/")[0]
    for index, family in enumerate(_FAMILY_PRIORITY):
        if head.startswith(family):
            return index
    return len(_FAMILY_PRIORITY)


@dataclass(frozen=True)
class Term:
    """一条标准术语。

    ``alternates`` 记录其它标准下的不同说法, 便于审计「同一个缩写在不同体系里
    译法不同」这类问题(见模块文档「已知的译法冲突」)。
    """

    term: str
    zh: str
    standard_id: str
    clause: str | None = None
    alternates: tuple[tuple[str, str], ...] = ()


def _t(term: str, zh: str, std: str, clause: str | None = None, **alt: str) -> Term:
    return Term(term=term, zh=zh, standard_id=std, clause=clause,
                alternates=tuple(alt.items()))


#: 已核实的术语。**每条都有出处** —— 没有出处的候选一律不收, 宁可让调用方
#: 回落到英文短名(那至少可追溯到公式 ID), 也不收一个来历不明的译名。
STANDARD_TERMS: tuple[Term, ...] = (
    _t("MTBF", "平均失效间隔时间", "GB/T 3187-1994", "7.2.8",
       DL_T_861="平均无故障工作时间"),
    _t("MTTF", "平均失效前时间", "GB/T 3187-1994", "7.2.7",
       DL_T_861="平均无故障工作时间"),
    _t("MTTR", "平均修复时间", "GB/T 3187-1994", "7.3.6"),
    _t("失效间隔时间", "平均失效间隔时间", "GB/T 2900.99-2016", "192-05-01"),
    _t("instantaneous failure rate", "瞬时失效率", "GB/T 2900.99-2016", "192-03-01"),
    _t("dependability", "可信性", "GB/T 2900.99-2016", "192-01-07"),
    # --- 电力半导体器件(GB/T 2900.32-1994 电工术语 电力半导体器件) ---
    # 该标准是本项目「器件级热学与可靠性术语」的权威出处。条号即原文编号。
    _t("thermal resistance", "热阻", "GB/T 2900.32-1994", "2.2.12"),
    _t("transient thermal impedance", "瞬态热阻抗", "GB/T 2900.32-1994", "2.2.13"),
    _t("thermal capacitance", "热容", "GB/T 2900.32-1994", "2.2.15"),
    _t("thermal derating factor", "热降额因数", "GB/T 2900.32-1994", "2.2.9"),
    _t("bulk lifetime", "体寿命", "GB/T 2900.32-1994", "2.1.24"),
    _t("case temperature", "管壳温度", "GB/T 2900.32-1994", "2.2.10"),
    _t("virtual junction temperature", "虚拟结温", "GB/T 2900.32-1994", "2.2.6",
       结温="等效结温"),
    # --- 电力电子技术(GB/T 2900.33-2004/IEC 60050-551:1998) ---
    # 等同采用 IEC 60050-551:1998, 术语编号与国际标准一致, 故可写 IEC 号备查。
    _t("power electronics", "电力电子学", "GB/T 2900.33-2004", "551-11-01"),
    _t("(electronics)(power)conversion", "变流", "GB/T 2900.33-2004", "551-11-02",
       换流="换流"),
    _t("(electronics)(power)switching", "电子通断", "GB/T 2900.33-2004", "551-11-03"),
    # --- 损耗与效率(GB/T 3859.1-2013 半导体变流器) ---
    _t("switching loss", "开关损耗", "GB/T 3859.1-2013", "7.4.1"),
    _t("conduction loss", "通态损耗", "GB/T 3859.1-2013", "7.4.1",
       IGBT通态损耗="IGBT 通态损耗"),
    _t("efficiency", "效率", "GB/T 3859.1-2013", "6.2.2"),
    _t("ripple voltage and current", "纹波电压和电流", "GB/T 3859.1-2013", "7.3.5"),
    _t("harmonic current", "谐波电流", "GB/T 3859.1-2013", "7.3.6"),
    # --- 阀损耗专项(GB/T 35702.1-2017) ---
    # 该标准专列「开关损耗」一章, 是本项目 MOSFET/IGBT 损耗口径最贴切���出处。
    _t("switching loss of valve", "开关损耗", "GB/T 35702.1-2017", "8"),
)


def resolve(term: str) -> Term | None:
    """按(术语, 标准优先级)取最权威的一条; 无出处则返回 ``None``。

    **找不到就返回 None, 不猜。** 调用方据此回落。

    公式短名是 ``SCREAMING_SNAKE``(如 ``SWITCHING_LOSS``), 而术语表里是
    小写带空格(``switching loss``), 所以先按原样查, 再按「下划线→空格 +
    转小写」规范化查。**只做大小写与分隔符的规范化** —— 不做截断、不做
    同义词猜测, 因为那会把不相关的术语匹配上。
    """
    normalized = term.replace("_", " ").strip().lower()
    hits = [
        t
        for t in STANDARD_TERMS
        if t.term == term or t.term.lower() == normalized
    ]
    if not hits:
        return None
    return min(hits, key=lambda t: (priority_of(t.standard_id), t.standard_id))
