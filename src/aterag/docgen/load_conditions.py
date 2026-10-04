"""工况限定词表:把「满载 / 半载 / xx%载」变成**可计算的无量纲标量**。

## 为什么需要它

方案里这批词只出现在散文的测试条件里(实测: 满载 8 次、半载 13 次、空载 1 次),
从未被结构化。后果是「效率公式在哪个工况下取值」这个问题**在数据里无处可答** ——
而 Semantica 的规则约束与推理恰好要这个: 同一型号在满载与半载下的效率不是同一个
数, 若不区分, 推理会拿错工况做代入。

## 归一化基准是满载

用户给的定义(可复述、可计算):

    半载   = 50%载 = 满载 × 50%
    xx%载  = 满载 × xx%

所以每个工况都是**相对满载的比例**, 满载自身为基准 ``1.0``。这一条把整批词统一成
无量纲标量, 于是

    P(半载) = ratio(半载) × P(满载) = 0.5 × P(满载)

成为一条**可推理**的等式, 而不是一个需要人去理解的形容词。

## 额定 与 标称 不是同一个概念 —— 本表刻意分开

「额定」(rated) 与「标称」(nominal) 在 GB/IEC 体系里含义不同: 额定值是器件/
工况的**保证极限或保证工作点**, 标称值是**常规取整的代表值**。方案里两者的
中文用法并不自洽(见 ``docs/w1-triage.md``: ``COND_VIN_NOM`` 的 ID 是 ``NOM``
而中文写作「额定输入电压」)。所以本表把它们建成**两类不同的限定词**, 而**不**
给它们编造比例值 —— 比例只对「载」类成立。

## 未收录即不猜

``resolve`` 找不到返回 ``None``, 调用方必须回落到原样保留限定词, 不许自行
折算。理由与 :mod:`aterag.docgen.standard_terms` 一致: 编一个比例值会让规则
在错误的工况上运行, 且这种错误不报错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "BASE_LOAD",
    "LoadCondition",
    "LOAD_CONDITIONS",
    "QUALIFIER_KINDS",
    "parse_percent_load",
    "resolve",
]


#: 归一化基准。满载自身的比例恒为 1.0 —— 「50%载」的 50% 是**相对满载**的,
#: 不是绝对输出功率的百分比。这个区别决定了乘谁。
BASE_LOAD = "满载"


@dataclass(frozen=True)
class LoadCondition:
    """一个工况限定词。

    :param ratio: 相对满载的比例; ``None`` 表示该词**不是**比例类限定词
        (如「额定」「标称」), 调用方不得对它做乘法。
    """

    zh: str
    en: str
    ratio: float | None
    #: 限定词类别: ``load``(载类, 有比例) / ``rating``(额定类) / ``nominal``(标称类)
    kind: str
    note: str = ""


#: 载类工况。比例取自用户给的定义(半载 = 满载 × 50%)。
#: 「最小载」在方案里出现 0 次, 但它是行业通用说法且量纲与半载同级, 故收录 ——
#: 收录为**待方案确认**: 它没有普适比例(最小稳定负载由具体变换器决定)。
LOAD_CONDITIONS: tuple[LoadCondition, ...] = (
    LoadCondition(
        zh="满载",
        en="full load",
        ratio=1.0,
        kind="load",
        note="归一化基准。任何 xx%载 都相对它折算。",
    ),
    LoadCondition(
        zh="半载",
        en="half load",
        ratio=0.5,
        kind="load",
        note="= 50%载 = 满载 × 50%。用户给定的定义。",
    ),
    LoadCondition(
        zh="空载",
        en="no load",
        ratio=0.0,
        kind="load",
        note="输出为 0。注意「空载」不等于「轻载」: 空载下开关频率常降频, "
        "损耗模型不适用同一套参数。",
    ),
    LoadCondition(
        zh="最小载",
        en="minimum load",
        ratio=None,
        kind="load",
        note="**比例待确认**: 最小稳定负载由具体变换器拓扑决定, 不是常数。"
        "收录但不给比例 —— 编一个数会让规则在错误工况上运行且不报错。",
    ),
)

#: 非比例类限定词。**刻意不给 ratio** —— 见模块文档「额定与标称不是同一概念」。
QUALIFIER_KINDS: tuple[LoadCondition, ...] = (
    LoadCondition(
        zh="额定",
        en="rated",
        ratio=None,
        kind="rating",
        note="保证工作点/保证极限。IEC 与 GB 体系里与「标称」不同, 不可互换。",
    ),
    LoadCondition(
        zh="标称",
        en="nominal",
        ratio=None,
        kind="nominal",
        note="常规取整的代表值, 无保证含义。",
    ),
    LoadCondition(
        zh="最大额定",
        en="absolute maximum rating",
        ratio=None,
        kind="rating",
        note="超过即可能损坏的绝对上限, 与「额定工作值」不是一回事。",
    ),
)


#: ``30%载`` / ``12.5%载`` / ``100%载`` 这类模式。**必须整串锚定** —— 否则
#: ``P_load`` 里的 ``load`` 会被当成工况词, 而它其实是符号的一部分。
_PERCENT_LOAD_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*%\s*载$")


def parse_percent_load(text: str) -> LoadCondition | None:
    """``30%载`` -> ``LoadCondition(ratio=0.30)``。不是这个形态返回 ``None``。

    ``100%载`` 与「满载」等价(比例 1.0), 但**保留原词**而不是改写成「满载」——
    改写会丢失方案原文, 而原文是审计依据。
    """
    match = _PERCENT_LOAD_RE.match(text.strip())
    if match is None:
        return None
    percent = float(match.group(1))
    if not 0.0 <= percent <= 200.0:
        # 超过 200% 不是载类工况, 是数据错误。不猜。
        return None
    return LoadCondition(
        zh=text.strip(),
        en=f"{percent:g}% load",
        ratio=percent / 100.0,
        kind="load",
        note=f"= {BASE_LOAD} × {percent:g}%。",
    )


def resolve(text: str) -> LoadCondition | None:
    """查一个工况限定词。**查不到返回 ``None``, 不猜。**"""
    if not text:
        return None
    candidate = text.strip()
    for item in (*LOAD_CONDITIONS, *QUALIFIER_KINDS):
        if item.zh == candidate or item.en.lower() == candidate.lower():
            return item
    return parse_percent_load(candidate)
