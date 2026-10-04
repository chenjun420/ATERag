"""命名约定 -> 量纲的**显式规则表**, 以及与 U.5 合并后的量纲字典。

为什么需要这一层 (以及它为什么必须显式)
--------------------------------------
W1 要求「公式入库 ≥ 400 条**全部** ``dimension_ok=true``」。实测: 545 个公式 ID
里 366 个能归一化成引擎语法, 而其中**只有 19 条**量纲闭合 —— 因为公式共引用
**491 个变量**, 而 U.5 只覆盖其中一小部分。

U.5 是方案自己声明的权威表, 优先于任何规则。但它覆盖不了, 所以量纲必须从
**命名约定**推出来 —— 而这就是推断, 不是抽取。推断必须**留痕**: 本模块的每条
规则都带 ``rule_id`` 与 ``rationale``, 解析结果 (:class:`Resolution`) 记录
**是哪条规则命中的**。没有任何变量允许「悄悄」拿到一个量纲。

三条自律
--------
1. **规则表是唯一推断来源。** 解析顺序固定为 U.5 命名空间精确命中 → U.5 全库
   唯一 → 规则表 (声明顺序, 先命中先赢) → 未解析。绝不在别处按前缀猜。
2. **宁可漏, 不可错。** 只收「在该领域里读音与量纲都稳定」的前缀。存在真实
   歧义的词干**故意不给规则**, 让公式因「变量未解析」而闭不上 —— 一个闭不上的
   公式会出现在报告里, 一个量纲算错的公式会安静地混进库里。见
   :data:`_DELIBERATELY_UNRULED`。
3. **例外写在前面。** ``R_*`` -> [Ω] 是通例, ``R_th``/``R_θ*`` -> [K/W] 是例外;
   顺序上例外必须先命中, 所以 :data:`RULES` 是「特例在前、通例在后」。

规则表要能被证伪
----------------
规则不是拍脑袋写的, 它们要过一道**独立校验**: 语料里有 114 条公式自带量纲列
(``[V]`` / ``[V]/([H]·f)`` 之类), 那是方案自己写下的答案。校验的做法是拿规则
推出的量纲与它对照 —— 规则与方案自己打架, 就是规则错了。见
``tests/unit/docgen/test_quantity_rules.py::TestRulesAgreeWithDeclaredDimensions``。

刻意不给规则的部分
------------------
以下词干在本领域里量纲不唯一, 给了规则就是伪造:

* ``T_*`` —— ``T_j``/``T_a`` 是温度 [K], ``T_s`` 是开关周期 [s]。U.5 用 ``[T]``
  表示时间, 用 ``K`` 表示温度, 两者撞在一个字母上。
* ``A_*`` —— 面积 [m²] 与「安培」都可能。
* ``H_*`` —— 电感 [H] 与磁场强度都可能。
* ``U_*`` / ``u_*`` —— U.5 把 ``U``/``u_c`` 列为相对不确定度 ``[X]``, 而语料里
  ``U_trip``/``U_nom`` 明显是电压。**方案自身在这点上是矛盾的**, 规则不能替它
  选一边。
* ``s`` / ``z`` / ``y`` / ``e`` —— 拉普拉斯变量、复变量、自然对数底, 量纲取决于
  上下文约定, 而方案没给约定。
* ``k_*`` —— 包含因子 [1]、下垂系数 [Ω]、玻尔兹曼常数 [eV/K]、波形系数都叫 k。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from ..solver.symbolic import DIMENSIONLESS, Dimension
from .symbols import SymbolTable, parse_spec_dimension

__all__ = ["QuantityRule", "QuantityDictionary", "Resolution", "RULES", "build_dictionary"]


@dataclass(frozen=True)
class QuantityRule:
    """一条命名约定规则。

    :param rule_id: 稳定标识。会写进 :class:`Resolution`, 便于事后追责。
    :param pattern: 正则源码, ``re.search`` 语义 (不要求全匹配)。
    :param dimension: 该规则给出的量纲。
    :param rationale: **为什么成立**。没有理由的规则不允许进表 —— 复核的人
        需要知道这条推断的依据是什么, 而不是只看它给出什么结果。
    :param namespaces: 非空时只在这些命名空间生效 (与公式所在附录的域字母匹配)。
    """

    rule_id: str
    pattern: str
    dimension: Dimension
    rationale: str
    namespaces: tuple[str, ...] = ()

    def matches(self, symbol: str, namespace: str | None) -> bool:
        if self.namespaces and namespace not in self.namespaces:
            return False
        return re.search(self.pattern, symbol) is not None


@dataclass(frozen=True)
class Resolution:
    """一次量纲解析的结果。**永远带来源**, 便于审计。"""

    #: 解析出的量纲; ``None`` 表示未解析。
    dimension: Dimension | None
    #: 来源: ``u5-namespace`` / ``u5-unique`` / ``rule:<rule_id>`` / ``""``。
    source: str = ""
    #: 未解析时的原因 (人类可读)。
    reason: str = ""

    @property
    def is_rule_derived(self) -> bool:
        return self.source.startswith("rule:")


def _rule(
    rule_id: str,
    pattern: str,
    dimension_text: str,
    rationale: str,
    namespaces: tuple[str, ...] = (),
) -> QuantityRule:
    dim = parse_spec_dimension(dimension_text)
    if dim is None:  # pragma: no cover - 只可能在开发期触发
        raise ValueError(f"规则 {rule_id} 的量纲记号无法解析: {dimension_text!r}")
    return QuantityRule(rule_id, pattern, dim, rationale, namespaces)


#: 规则表。**特例在前, 通例在后** —— 顺序即优先级。
RULES: tuple[QuantityRule, ...] = (
    # ---- 热阻类特例: 必须在 R_ 通例之前 ----
    _rule("thermal-resistance", r"^R_(θ|th|θjc|θcs|θsa)", "[K/W]",
          "热阻 = 温升/功耗。R_θ* 与 R_th 都是热阻, 与电阻同字母但量纲不同 —— "
          "这是 R 前缀唯一确定会出错的子类, 故单列且排在通例前。"),
    _rule("capacitance-thermal", r"^C_(th|θ)", "[J/K]",
          "热容 = 热量/温升。R_θ·C_th 正是热时间常数 τ_th, 与电容 [F] 不同。"),
    # ---- 明确的精确条目 ----
    _rule("pf", r"^(PF|PF_?THD|THD|THD_I)$", "[1]",
          "功率因数与总谐波畸变都是比值 (有功/视在、谐波/基波), 无量纲。"),
    _rule("bandwidth", r"^(BW|f_3dB)$", "[Hz]",
          "带宽是频率的区间宽度, 量纲与频率相同。U.5 未收录这两个符号。"),
    _rule("reliability-rate", r"^(PFH|PFD|SFF|FIT)$", "[T⁻¹]",
          "IEC 61508 定义的每小时危险失效频率, 量纲是时间倒数。记号照抄 U.5 自己"
          "用于失效率的 ``[T⁻¹]`` (而不是 ``[1/h]``) —— ``h`` 在量纲记号里既可能"
          "是小时也可能是普朗克常数, 不引入它就少一个歧义源。U.5 把这几个符号写成"
          "「见各条」, 而它们在 M/L 命名空间的公式里必须参与齐次性判定。"),
    # ---- 电容等效串联电阻 ----
    _rule("esr", r"^ESR$", "[Ω]",
          "等效串联电阻。GB/T 2900.16《电工术语 电力电容器》与 GB/T 6346.1-2024"
          " 6.4「损耗角正切和等效串联电阻(ESR)」均定义该术语, 物理含义是电容"
          "等效电路中串联电阻的阻值, 故为电阻 [Ω]。自洽校验: ESR = D/(ωC), "
          "D 无量纲、ωC 无量纲, 量纲自洽。U.5 完全未收录该符号(正文出现 69 次)。"),
    # ---- 电感电流纹波 ----
    _rule("inductor-ripple-current", r"^ΔI_L$", "[A]",
          "电感电流纹波(峰峰值)。依据是方案自己在公式表的结果量纲列给出的推导: "
          "``ΔI_L = (V_on·D)/(L·f_sw)`` 的结果是 ``[V]/([H]·[Hz]) = [A]`` —— "
          "U.5 未收录, 但方案的量纲列已给出完整推导, 无需外部查证。"),
    # ---- 相位差 ----
    _rule("phase-delta", r"^Δφ$", "[1]",
          "相位差。由方案自身的式子 ``Δφ = 360°·f/f_sw`` 推出: f/f_sw 是同量纲"
          "之比(无量纲), 360° 是角度的整倍数(无量纲), 故 Δφ 无量纲(弧度制下"
          "按弧度计入)。不能写成 [°]: 那会让 360° 因子重复计入一次。"),
    # ---- 交叉调整率 ----
    _rule("cross-regulation", r"^CR$", "[1]",
          "交叉调整率。由 ``CR = ΔV_aux/V_aux,nom × 100%`` 推出 —— 电压之比乘"
          "百分比, 无量纲。方案该行的结果量纲列也写作「无量纲」, 两处一致。"),
    # ---- 额定/实际温度 ----
    _rule("temperature-rated-actual", r"^T_(rated|actual|nom|max|j|a|amb)$", "[K]",
          "温度。由方案自身的式子 ``L = L₀·2^((T_rated−T_actual)/10)·"
          "(V_rated/V_actual)^k`` 推出: 指数 ``(T_rated−T_actual)/10`` 必须无量纲, "
          "故差值为 [K](分母 10 带 K 量纲)。U.5 把这几个符号写成「见各条」。"
          "注意与裸 ``T`` 区分 —— ``T_j`` 是温度而 ``T_s`` 是开关周期, "
          "见 :data:`_DELIBERATELY_UNRULED`。"),
    # ---- 信噪比类 ----
    _rule("ratio-db", r"^(SNR|SINAD|ENOB|THD|PF)$", "[1]",
          "信噪比/失真-噪声比/有效位数/总谐波畸变/功率因数, 全部是比值, 无量纲。"
          "U.5 把 SNR/ENOB/SINAD 写成「见各条」。注意若后续公式改用 20log10() "
          "形式给出 dB 值, 则需另立 [dB] 规则 —— 两种写法量纲不同, 不能混。"),
    # ---- 电压 ----
    _rule("voltage", r"^V(_|$)", "[V]",
          "V 词干在电气域一律电压 (V_in/V_out/V_rms/V_peak/V_th); U.5 已命中的"
          "优先, 这里只补 U.5 没收录的。"),
    _rule("voltage-delta", r"^ΔV(_|$)", "[V]",
          "电压增量。Δ 不改变量纲, 故与 V 同。"),
    # ---- 电流 ----
    _rule("current", r"^I(_|$)", "[A]",
          "I 词干一律电流 (I_load/I_rms/I_pk/I_set)。"),
    _rule("current-lower", r"^i(_|$)", "[A]",
          "小写 i 在本方案里是瞬时电流 (i_L/i_pk/i_n)。"),
    # ---- 功率 ----
    _rule("power", r"^P(_|$)", "[W]",
          "P 词干一律功率 (P_load/P_loss/P_cond/P_gate)。"),
    # ---- 频率与时间 ----
    _rule("frequency", r"^f(_|$)", "[Hz]",
          "f 词干一律频率 (f_sw/f_s/f_max/f_clk)。"),
    _rule("angular-frequency", r"^ω(_|$)", "[Hz]",
          "角频率, 量纲仍是时间倒数, 与 Hz 同维 (差一个 2π 是量值不是量纲)。"),
    _rule("time", r"^t(_|$)", "[s]",
          "小写 t 一律时间 (t_s/t_pk/t_margin/t_trip)。**大写 T 不适用**, 见模块"
          " docstring: T_j 是温度而 T_s 是开关周期。"),
    _rule("time-constant", r"^τ(_|$)", "[s]",
          "时间常数, 量纲就是时间。"),
    _rule("efficiency", r"^η(_|$)", "[1]",
          "效率是输出功率与输入功率之比, 是比值, 无量纲。η_max/η_target 同理。"),
    # ---- 无源元件 ----
    _rule("impedance", r"^Z(_|$)", "[Ω]",
          "Z 词干在本方案里是阻抗 (Z_in/Z_out/Z_L/Z_0), 不出现别的含义。"),
    _rule("inductance", r"^L(_|$)", "[H]",
          "L 在电气域是电感 (L_1/L_2/L_lk/L_par)。**已排除** L_n 作为「相位裕度」"
          "的读法 —— 那只出现在控制域且不带下标, U.5 已单列, 规则不与之争。"),
    _rule("capacitance", r"^C(_|$)", "[F]",
          "C 在电气域是电容 (C_out/C_oss/C_eff/C_GD)。热容已由 capacitance-thermal "
          "在前排除。"),
    _rule("resistance", r"^R(_|$)", "[Ω]",
          "R 在电气域是电阻。热阻已由 thermal-resistance 在前排除。"),
    # ---- 计数与无量纲 ----
    _rule("count-phases", r"^N(_|$)", "[1]",
          "N 是相数/脉冲数/采样数, 是计数, 无量纲。"),
    _rule("turns-ratio", r"^n(_|$)", "[1]",
          "匝比/节点数, 无量纲。"),
)

#: 刻意不给规则的词干及原因。列出它们本身就是交付物的一部分 ——
#: 「为什么这些没覆盖」必须能回答, 否则「未解析」就只是一个数字。
def _require_dimension(unit_expr: str) -> Dimension:
    """解析量纲记号, 解析不出就**当场报错**。

    公式级覆盖表里的量纲是字面量, 必然可解析; 若真解析不出, 说明表写错了,
    必须在 import 时炸掉, 而不是塞一个 ``None`` 让下游拿到「无量纲」——
    那正是「看起来对但错」的来源。
    """
    dim = parse_spec_dimension(unit_expr)
    if dim is None:
        raise ValueError(f"公式级覆盖的量纲记号无法解析: {unit_expr!r}")
    return dim


#: **公式级量纲覆盖**: ``(公式 ID, 符号)`` -> ``(量纲, 理由)``。
#:
#: 只处理**同名不同物**的碰撞 —— 即 U.5 的定义本身没错, 但在这条公式的语境里
#: 是另一个物理量。改全局定义会把 U.5 那条正确的定义改错, 所以覆盖必须**窄到
#: 单条公式**。
#:
#: 目前唯一一处: ``R_th``。U.5 收录的是**热阻** [K/W], 而 ``F_W.1.10_NORTON``
#: 的诺顿等效里它是**等效电阻** [Ω] —— 实测原式 ``V_th/R_th`` 被解析成
#: V/(K/W), 该公式因此永不可能齐次。
FORMULA_SCOPED: Mapping[tuple[str, str], tuple[Dimension, str]] = {
    ("F_W.1.10_NORTON", "R_th"): (
        # ``parse_spec_dimension`` 返回 ``Dimension | None``; 此处字面量必然可解析,
        # 但类型上仍需收窄 —— 解析不出就当场报错, 不能悄悄塞 None。
        _require_dimension("[Ω]"),
        "诺顿等效中 R_th 为等效电阻 [Ω], 非热阻 [K/W]; U.5 的热阻定义对热学语境仍然正确",
    ),
    # ---- 只在本式语境下成立的符号: 必须公式级, 不能进通例 ----
    # 裸 ``k`` 有四种含义(含容因子/下垂系数/玻尔兹曼常数/波形系数), 裸 ``s`` 的
    # 量纲取决于方案未给出的约定 —— 两者都在 :data:`_DELIBERATELY_UNRULED` 里。
    # 但**在这两个具体式子里**, 齐次性要求把它们钉死:
    ("F_N.5.1_CAP_LIFE", "k"): (
        _require_dimension("[1]"),
        "仅作指数 ``(V_rated/V_actual)^k`` 出现, 底数是同量纲电压之比(无量纲), "
        "故指数必须无量纲。与裸 k 的其它三种含义无关, 故只在本式生效。",
    ),
    ("F_K.4.2_TYPE_II", "s"): (
        _require_dimension("[T⁻¹]"),
        "式子 ``G_c(s) = K_c·(1+s/ω_z)/[s(1+s/ω_p)]`` 中 ``s/ω_z`` 必须是 ``1+`` "
        "里的无量纲比值, 故 s 与角频率 ω 同量纲 [T⁻¹]。**不是**无量纲: 若取无量纲, "
        "则整个传递函数不再齐次。这正是控制理论中 s 取 [T⁻¹] 的原因。",
    ),
    ("F_K.4.2_TYPE_II", "K_c"): (
        _require_dimension("[1]"),
        "补偿系数。式中 K_c 单独出现且整体为传递函数, 由齐次性要求 K_c 无量纲。"
        "方案未声明, 也不属于任何标准术语, 属推导所得。",
    ),
}


_DELIBERATELY_UNRULED: Mapping[str, str] = {
    "T": "T_j/T_a 是温度 [K] 而 T_s 是开关周期 [s]; U.5 用 [T] 表示时间, 字母撞车",
    "A": "面积 [m²] 与安培 [A] 都可能 (A_core 是面积, A_out 若存在则可能是电流)",
    "H": "电感 [H] 与磁场强度都可能",
    "U": "U.5 把 U/u_c 列为相对不确定度 [X], 而 U_trip/U_nom 明显是电压 —— 方案自相矛盾",
    "u": "同 U: 相对不确定度 [X] 与电压 [V] 都可能",
    "s": "拉普拉斯变量, 量纲取决于方案未给出的约定",
    "z": "复变量/Laplace 域变量, 同上",
    "y": "同上",
    "e": "自然常数底与电荷量都可能",
    "k": "包含因子 [1]/下垂系数 [Ω]/玻尔兹曼常数 [eV/K]/波形系数都叫 k",
    "x": "自变量, 量纲取决于具体公式",
}


@dataclass
class QuantityDictionary:
    """U.5 符号表 + 命名约定规则表。

    :param u5: U.5 抽出的符号表 (:mod:`aterag.docgen.symbols`)。
    :param rules: 规则表, 默认 :data:`RULES`。
    """

    u5: SymbolTable
    rules: tuple[QuantityRule, ...] = RULES

    def resolve(
        self, symbol: str, namespace: str | None, formula_id: str | None = None
    ) -> Resolution:
        """解 ``(符号, 命名空间)`` -> :class:`Resolution`。

        顺序: **公式级覆盖** → U.5 命名空间精确 → U.5 全库唯一 → 规则表。
        **不做前缀回退** —— U.5 自己警告过裸符号歧义, 回退等于把它又引回来。

        **公式级覆盖**(:data:`FORMULA_SCOPED`)优先于一切, 因为它处理的是
        **同名不同物**的碰撞: ``R_th`` 在热学语境确是热阻 [K/W](U.5 没写错),
        但在诺顿等效里它是等效电阻 [Ω]。改全局定义会把热阻那条改错;
        不处理则该公式量纲永不可能齐次。

        **U.5 写「见各条」时不拦截, 继续问规则表。** 「方案没写」不等于「方案
        禁止」: U.5 把 ``PFH``/``PFD``/``SFF``/``DC``/``ENOB`` 等写成「见各条」,
        早先一版在这里直接返回未解析, 于是 reliability-rate 规则**一次都命中
        不到** (死规则)。让规则补上, 且来源仍标成 ``rule:`` —— 审计得见它
        不是 U.5 说的。

        只有 U.5 **给出了互相矛盾**的量纲才停: 那说明方案自身冲突, 规则无权
        在其中选边 (例如 ``k`` 在 M 是 [1]、在 Q 是 [Ω])。
        """
        if formula_id is not None:
            override = FORMULA_SCOPED.get((formula_id, symbol))
            if override is not None:
                return Resolution(override[0], f"formula-scoped:{formula_id}")
        entry = self.u5.resolve(symbol, namespace)
        if entry is not None and entry.dimension is not None:
            return Resolution(entry.dimension, "u5-namespace")

        cands = self.u5.by_symbol.get(symbol, ())
        if len(cands) == 1:
            if cands[0].dimension is not None:
                return Resolution(cands[0].dimension, "u5-unique")
        elif len(cands) > 1:
            dims = {c.dimension for c in cands}
            if len(dims) == 1 and None not in dims:
                # 多条条目但量纲完全一致 —— 不存在歧义, 可以用
                return Resolution(dims.pop(), "u5-unique")
            return Resolution(None, "", f"U.5 中 {symbol} 有多个互不相同的量纲")

        for rule in self.rules:
            if rule.matches(symbol, namespace):
                return Resolution(rule.dimension, f"rule:{rule.rule_id}")

        if cands:
            return Resolution(None, "", f"U.5 有 {symbol} 但量纲未定, 且无规则覆盖")
        return Resolution(None, "", _unresolved_reason(symbol))

    def rule_usage(self, symbols: set[str], namespace_of: dict[str, str | None]) -> dict[str, int]:
        """统计每条规则实际命中了多少符号 —— 用来发现「一条规则命中 0 个」的死规则。"""
        used: dict[str, int] = {}
        for sym in symbols:
            res = self.resolve(sym, namespace_of.get(sym))
            if res.is_rule_derived:
                key = res.source.split(":", 1)[1]
                used[key] = used.get(key, 0) + 1
        return used


def _unresolved_reason(symbol: str) -> str:
    stem = symbol.split("_")[0]
    if stem in _DELIBERATELY_UNRULED:
        return f"{stem} 词干量纲不唯一, 刻意不给规则: {_DELIBERATELY_UNRULED[stem]}"
    return "命名约定未覆盖 (无对应规则)"


def build_dictionary(u5: SymbolTable) -> QuantityDictionary:
    """组装最终字典。纯函数。"""
    return QuantityDictionary(u5=u5)


def unruleable_stems() -> Mapping[str, str]:
    """刻意不给规则的词干 -> 原因。供报告与门禁 G7 使用。"""
    return dict(_DELIBERATELY_UNRULED)


def dimensionless_ok(dim: Dimension | None) -> bool:
    return dim is not None and dim == DIMENSIONLESS
