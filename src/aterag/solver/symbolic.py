"""L3 符号化与量纲引擎。

依据 V6.0:
    原则二      所有数值/单位/区间/可行性判定必须出 LLM 之外
    §18.2.3     solver.symbolic 接口契约
    §18.3.1     formula.dimension_vec —— 7 维量纲向量 (A-11)
    §18.9 G1    量纲不齐的公式不得入库
    R1          量纲齐次性

本模块是 ADR-015 的执行体。它替换 `src/aterag/inference/engine.py:106,187`
的 ``eval()`` + 正则黑名单 —— 那条路径没有单位概念, 因此
``P = U*I`` 与 ``P = U*I*R`` 都能算出数, 而后者量纲错误。

7 维向量 (A-11 的具体化)
-----------------------
方案只说「7 维量纲向量」, 未定义是哪 7 维。本模块的取法:

    [0] length      [1] mass      [2] time      [3] electric_current
    [4] temperature [5] amount    [6] luminous_intensity

即 SI 的七个基本量。这不是随意选的: 任何物理量的量纲都能写成这 7 个基本量
的整数幂乘积, 而电力电子关心的量 (电压/电流/功率/频率/阻抗/电感/电容/
能量/时间) 全部落在这个空间里。选别的维度集合 (比如工程单位
V/A/Ω/W/Hz/S) 会有两个问题: 它们之间不是线性独立的 (W = V·A, Ω = V/A),
向量比较失去意义; 且需要每种量纲组合一张对照表, 而那正是要消除的手工维护。

量纲同余判定
------------
两条表达式量纲相同 <=> 7 维向量逐分量相等。判断依据是量纲的乘法性质:
``dim(a*b) = dim(a) + dim(b)``, ``dim(a/b) = dim(a) - dim(b)``,
``dim(a^b) = b * dim(a)``, ``dim(f(a))`` 取 f 自带量纲。

对 ``x**2`` 这类变量自身指数: 数学上 dim(x^2) = 2·dim(x), 但 x 的量纲未知,
因此不能据此判断齐次。处理办法是要求**幂必须是无量纲数** (指数为纯数值),
否则拒绝。对应到方案里: 物理公式中的指数几乎总是无量纲的
(``exp(-t/τ)`` 的指数), 出现 ``U^n`` 且 n 带单位才是量纲错误。

无量纲常数
----------
表达式里出现的 2、3、π、e 这类裸数一律视为无量纲。方案 §18.10 注 1 特别
警告过要避免「硬编码 50、20MHz」这类经验值 —— 但纯数系数本身是合法的
(欧姆定律里的系数 1、RC 时间常数的系数 1)。区分标准是: 裸数只能是
无量纲的量, 任何带单位的数 (50 V、20 MHz) 必须写成带单位的形式。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

# 7 维向量的分量名。顺序即 schema_full.sql 里 dimension_vec 的下标顺序,
# 一旦落库就不能改 —— 改了等于让全部既有公式的 dimension_vec 失效。
DIMENSION_COMPONENTS: tuple[str, ...] = (
    "length",
    "mass",
    "time",
    "current",
    "temperature",
    "amount",
    "luminous_intensity",
)

#: 人类可读的中文名, 用于门禁报错信息。
DIMENSION_LABELS_ZH: Mapping[str, str] = {
    "length": "长度",
    "mass": "质量",
    "time": "时间",
    "current": "电流",
    "temperature": "热力学温度",
    "amount": "物质的量",
    "luminous_intensity": "发光强度",
}

#: pint 的基本量名 -> 本模块的 7 维分量名。
#:
#: 两者在两个分量上不一致, 且都是 pint 侧的既定命名:
#:   luminous_intensity -> pint 叫 ``luminosity`` (光度学里的叫法)
#:   amount             -> pint 叫 ``substance``   (IUPAC 的叫法)
#: 不做这层映射的话这两个量纲会被当成「7 维之外」而拒绝, 于是坎德拉
#: 与开尔文并列的标准量被误判成非法。
#: 键是 pint 名, 值是 DIMENSION_COMPONENTS 里的名字。
_PINT_BASE_ALIASES: Mapping[str, str] = {
    "luminosity": "luminous_intensity",
    "substance": "amount",
}


class SymbolicError(RuntimeError):
    """符号化失败。"""


class ExpressionError(SymbolicError):
    """表达式非法 (语法错、未知变量、非法幂)。"""


class DimensionError(SymbolicError):
    """量纲不齐。

    这是 R1 的执行点。抛出而非警告 —— §18.10 注 1 要求宁可 400 条
    量纲闭合的公式, 也不要 1000 条混着量纲错的。
    """


@dataclass(frozen=True)
class Dimension:
    """7 维量纲向量。

    ``frozen=True``: 量纲向量会被存进数据库、跨模块传递、参与相等判定。
    可变的量纲向量会让「比较两个量纲是否相同」变成误操作。

    分量类型是 ``float`` 而非 ``int``, 因为 sqrt 会把分量折半
    (sqrt(电阻) 是对数坐标下的几何均值阻抗, 真实用途), 折半结果不必是
    整数。对应地 ``formula.dimension_vec`` 是 ``NUMERIC(8,4)[]`` 而不是
    int[] —— 方案 §18.3.1 选 NUMERIC 而非 INT 是对的。

    ``has_fractional_parts`` 让「需要人工复核」与「正常」可区分:
    非整数分量几乎只来自开方形式, 是真公式但少见。
    """

    length: float = 0.0
    mass: float = 0.0
    time: float = 0.0
    current: float = 0.0
    temperature: float = 0.0
    amount: float = 0.0
    luminous_intensity: float = 0.0

    def __add__(self, other: Dimension) -> Dimension:
        return Dimension(
            self.length + other.length,
            self.mass + other.mass,
            self.time + other.time,
            self.current + other.current,
            self.temperature + other.temperature,
            self.amount + other.amount,
            self.luminous_intensity + other.luminous_intensity,
        )

    def __sub__(self, other: Dimension) -> Dimension:
        return Dimension(
            self.length - other.length,
            self.mass - other.mass,
            self.time - other.time,
            self.current - other.current,
            self.temperature - other.temperature,
            self.amount - other.amount,
            self.luminous_intensity - other.luminous_intensity,
        )

    def __mul__(self, k: float) -> Dimension:
        return Dimension(
            self.length * k,
            self.mass * k,
            self.time * k,
            self.current * k,
            self.temperature * k,
            self.amount * k,
            self.luminous_intensity * k,
        )

    def __neg__(self) -> Dimension:
        return self * -1.0

    @property
    def is_dimensionless(self) -> bool:
        return self.as_tuple() == (0.0,) * len(DIMENSION_COMPONENTS)

    @property
    def has_fractional_parts(self) -> bool:
        """是否有非整数分量。

        只有这类量纲需要人工复核: 常见于几何均值 (sqrt) 与开方形式的
        等效电路参数。G1 门禁可据此把「量纲齐次但需复核」与
        「量纲齐次且正常」区分开。
        """
        return any(abs(v - round(v)) > 1e-9 for v in self.as_tuple())

    def as_tuple(self) -> tuple[float, ...]:
        """按 DIMENSION_COMPONENTS 顺序展开。"""
        return (
            self.length,
            self.mass,
            self.time,
            self.current,
            self.temperature,
            self.amount,
            self.luminous_intensity,
        )

    def is_compatible_with(self, other: Dimension) -> bool:
        """量纲相容性判定。

        字符串解析相容性在此处**故意不做**: 相容只允许乘除与无量纲幂,
        加减两侧必须严格同量纲。允许「相容但不等」会把
        ``U + I`` 这种明显错误放行 —— U 是伏特、I 是安培, 加出来没有物理意义。
        """
        return self.as_tuple() == other.as_tuple()

    def describe(self) -> str:
        """人类可读描述, 用于报错信息。"""
        if self.is_dimensionless:
            return "无量纲 [1]"
        parts: list[str] = []
        for name, value in zip(DIMENSION_COMPONENTS, self.as_tuple(), strict=True):
            if value == 0:
                continue
            label = DIMENSION_LABELS_ZH[name]
            exponent = int(value) if abs(value - round(value)) < 1e-9 else value
            parts.append(f"{label}^{exponent}" if exponent != 1 else label)
        return " * ".join(parts)

    @staticmethod
    def from_tuple(values: tuple[float, ...]) -> Dimension:
        if len(values) != len(DIMENSION_COMPONENTS):
            raise SymbolicError(
                f"量纲向量必须是 {len(DIMENSION_COMPONENTS)} 维, 收到 {len(values)} 维: {values}"
            )
        return Dimension(*values)


DIMENSIONLESS = Dimension()


@lru_cache(maxsize=1)
def _registry() -> Any:
    """惰性构造 pint 注册表。

    冷启动 100~300 ms。放函数内 + lru_cache 而非模块级:
    import 本模块不应该付出这个代价 —— 只有真的要算量纲时才付。
    """
    import pint

    ureg = pint.UnitRegistry()

    # 电源领域的别名。方案 §6.2 的术语表大量使用工程写法, 缺了这些
    # 同一个物理量会因写法不同被判成不同量纲。
    ureg.define("@alias ohm = ohm = OHM = ohm_s = R = ohm")  # noqa: FLY002
    ureg.define("@alias V = volt = volt = VDC = volts = v_rms")  # noqa: FLY002
    ureg.define("@alias A = ampere = ampere = A_DC = amps")  # noqa: FLY002
    ureg.define("@alias F = farad = farad = uF = nF = pF = u_farad")  # noqa: FLY002
    ureg.define("@alias H = henry = henry = uH = mH = u_henry")  # noqa: FLY002
    ureg.define("@alias W = watt = watt = mW = uW = kW")  # noqa: FLY002
    ureg.define("@alias Hz = hertz = hertz = kHz = MHz = GHz")  # noqa: FLY002
    # 电源工程常用而 SI 没有的量
    ureg.define("switch = 1 = cycle")  # noqa: FLY002
    ureg.define("VA = volt * ampere = VAs = VA")  # noqa: FLY002
    ureg.define("var = volt * ampere = vars = VAR")  # noqa: FLY002
    ureg.define("percent = 0.01 = pct = percent")  # noqa: FLY002
    ureg.define("ppm = 1e-6 = ppm")  # noqa: FLY002

    return ureg


def unit_registry() -> Any:
    """取进程内唯一的 pint 注册表。"""
    return _registry()


def to_base_units(magnitude: float, unit: str) -> float:
    """把带前缀的量换算成该单位制的基本单位。

    ``to_base_units(1000, "mA")`` -> ``1.0``。

    用于求值: 内部一律用基本单位, 这样不同前缀的同一个物理量相加时
    不会相差 1000 倍。若直接用字面量相加, ``1 A + 1000 mA`` 会得到 1001
    而非 2 —— 这是量纲引擎不处理前缀时的经典静默错误。
    """
    ureg = _registry()
    try:
        quantity = ureg.Quantity(magnitude, unit)
        return float(quantity.to_base_units().magnitude)
    except Exception as exc:
        raise ExpressionError(f"单位无法换算: {magnitude} {unit!r} ({exc})") from exc


def unit_dimension(unit_expr: str) -> Dimension:
    """把单位表达式转成 7 维量纲向量。

    :param unit_expr: pint 单位表达式, 如 ``"V"``、``"V/A"``、``"1/s"``
    :raises ExpressionError: 单位无法解析
    """
    ureg = _registry()
    try:
        quantity = ureg.Quantity(1, unit_expr)
    except Exception as exc:  # pint 的异常类型随版本变, 不逐个枚举
        raise ExpressionError(f"单位无法解析: {unit_expr!r} ({exc})") from exc
    return _pint_dim_to_dimension(quantity.dimensionality, unit_expr)


def _pint_dim_to_dimension(dimensionality: Any, source: str) -> Dimension:
    """pint 的 UnitsContainer -> 7 维向量。

    先把 7 个基本量的指数读出来, 再检查是否剩下别的量纲因子。
    剩下因子说明该量纲超出电力电子关心的范围 (如 ``candela`` 之外的
    ``lumen`` 之外的记法, 或拼写错误)。此时必须报错而不是忽略 ——
    忽略会把拼错的单位当成无量纲, 让 ``E = m*c^2`` 里的 c 变成裸数。
    """
    exponents: dict[str, int] = {}
    leftovers: list[str] = []

    for unit_name, power in dimensionality.items():
        # pint 把基本量名写成 `[mass]` 这种带方括号的形式 (UnitsContainer 的
        # 键约定)。必须剥掉括号再比对, 否则每个基本量都会被误判成
        # 「7 维之外」而拒绝。
        bare = unit_name.strip("[]")
        canonical = _PINT_BASE_ALIASES.get(bare, bare)
        if canonical in DIMENSION_COMPONENTS:
            exponents[canonical] = int(power)
        else:
            # 派生单位 (V / A / ohm) 已被 pint 展开为基本量, 剩下的才是问题项
            leftovers.append(f"{bare}^{power}")

    if leftovers:
        raise ExpressionError(
            f"单位 {source!r} 含 7 维之外的量纲因子: {', '.join(leftovers)}。"
            f" 7 维取 SI 基本量 {DIMENSION_COMPONENTS}。"
            " 若这是拼写错误请修正; 若确实是本系统关心的量, 需先扩展 "
            "DIMENSION_COMPONENTS —— 而那会使已入库的 dimension_vec 全部失效。"
        )

    return Dimension.from_tuple(tuple(exponents.get(name, 0) for name in DIMENSION_COMPONENTS))


@dataclass(frozen=True)
class VariableSpec:
    """变量声明。

    ``dimension`` 显式给定而不是靠表达式推导 —— 变量的量纲是外部事实
    (「这个变量是输出电压」), 不该从公式里反推。反推会把
    ``R = U/I`` 里的 R 推成电压/电流, 而它其实是电阻。
    """

    name: str
    dimension: Dimension
    unit: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class DimensionCheckResult:
    """量纲校验结果。

    ``largest_dimension_ok`` 取各变量中「非无���纲」的那个 —— 公式的
    量纲就是这个。全部变量都无量纲时结果也是无量纲。
    变量之间量纲不一致 (一个电压一个电流混在同一个式子里) 是另一类错误,
    由 :func:`check_expression` 在求值阶段检出, 不在这里。
    """

    formula_id: str
    dimension: Dimension
    dimension_ok: bool
    #: 公式左侧 (结果) 的量纲, 若声明了 lhs
    lhs_dimension: Dimension | None = None
    #: 失败原因, dimension_ok 为 False 时非空
    reason: str | None = None

    def dimension_vec(self) -> list[float]:
        """转成 NUMERIC(8,4) 形状的列表。

        用 float 而非 Decimal: psycopg 写 NUMERIC 接受 Decimal 与 float,
        而 float 的 -1/2 这类指数是精确的, 转 Decimal 无收益。
        """
        return [float(v) for v in self.dimension.as_tuple()]


def check_expression(
    expression: str,
    variables: Mapping[str, VariableSpec],
    *,
    formula_id: str = "<unnamed>",
    lhs_dimension: Dimension | None = None,
) -> DimensionCheckResult:
    """校验表达式量纲齐次性。

    齐次性定义: 表达式中所有**加法项**的量纲相同, 且 (若给了
    ``lhs_dimension``) 与左侧量纲相同。

    为什么只对加法项要求同量纲: ``U*I + U^2/R`` 是齐次的,
    两项都是瓦特; 而 ``U + I`` 不齐次, 两项分别是伏特和安培。
    乘法项可以自由混合量纲 —— 乘法的结果量纲是各因子量纲之和, 这正是
    乘法的物理含义。

    :param expression: infix 表达式, 如 ``"U*I"``、``"U/I"``、``"(U*I)**0.5"``
    :param variables: 变量名 -> 声明
    :param lhs_dimension: 公式结果应有的量纲。给了才校验完整齐次性。
    :raises ExpressionError: 表达式语法错、未知变量、非法幂
    """
    ast = _parse(expression)
    _check_ast(ast, variables, expression)

    expr_dim = _dimension_of(ast, variables)
    ok = True
    reason: str | None = None

    if lhs_dimension is not None and not expr_dim.is_compatible_with(lhs_dimension):
        ok = False
        reason = (
            f"表达式量纲 {expr_dim.describe()} 与声明的结果量纲 {lhs_dimension.describe()} 不一致"
        )

    return DimensionCheckResult(
        formula_id=formula_id,
        dimension=expr_dim,
        dimension_ok=ok,
        lhs_dimension=lhs_dimension,
        reason=reason,
    )


def assert_dimension_ok(
    expression: str,
    variables: Mapping[str, VariableSpec],
    *,
    formula_id: str = "<unnamed>",
    lhs_dimension: Dimension | None = None,
) -> DimensionCheckResult:
    """同 check_expression, 但不齐次时抛 DimensionError。

    入库路径用这个 (fail-closed), 检索/展示路径用 check_expression
    (不抛, 好把 reason 显示出来)。
    """
    result = check_expression(
        expression, variables, formula_id=formula_id, lhs_dimension=lhs_dimension
    )
    if not result.dimension_ok:
        raise DimensionError(
            f"公式 {formula_id} 量纲不齐: {result.reason}。"
            f" 表达式: {expression}"
            " §18.9 G1: 量纲不齐的公式不得入库。"
        )
    return result


# ---- 表达式解析与求值 ----
# 自建最小 AST 而不用 ast.parse 的原因: 需要在**语法层**拒绝白名单外的节点,
# 而不是解析成功后再遍历。sympy 的 parse_expr 会接受 lambda 与属性访问,
# 那些不该出现在公式里。


@dataclass(frozen=True)
class Num:
    value: float
    dimension: Dimension


@dataclass(frozen=True)
class Var:
    name: str
    dimension: Dimension


@dataclass(frozen=True)
class BinOp:
    op: str
    left: Any
    right: Any


@dataclass(frozen=True)
class UnaryOp:
    op: str
    operand: Any


#: Python ``ast`` 的 BinOp 节点类名。键必须与 ``type(node.op).__name__``
#: 完全一致 —— 注意乘法在 ast 里叫 ``Mult`` 而不叫 ``Mul``, 写错会
#: 让所有含乘法的公式 (几乎全部) 直接语法检查失败。
_BINOPS: frozenset[str] = frozenset({"Add", "Sub", "Mult", "Div", "Pow"})

#: 一元负号是唯一允许的一元运算符。
#: 刻意不支持一元正号: 它不改变任何东西, 出现在公式里通常是笔误。
_UNARYOPS: frozenset[str] = frozenset({"USub"})

#: 允许的函数白名单。
#:
#: 刻意不含 exp / log / sin / cos: 它们对**带量纲**的参数无定义 ——
#: exp(1 V) 没有意义。而电源公式里这类函数几乎总是出现在无量纲指数形式:
#: exp(-t/tau)、exp(-2*pi*f*R*C)。正确写法是让比值无量纲, 然后才允许
#: 指数函数。
#:
#: 若哪天确需带量纲参数, 正确做法不是放宽白名单, 而是先在变量声明里
#: 造一个无量纲组合变量 —— 让无量纲化在声明处显式发生, 而不是藏在
#: 函数调用里。
_FUNCS: frozenset[str] = frozenset(
    {"sqrt", "abs", "min", "max", "exp", "log", "ln", "log10", "integral"}
)

#: **实参必须无量纲**的函数。``exp``/``log`` 的定义域是无量纲数, 这是数学事实而
#: 不是建模选择: ``exp(带量纲量)`` 没有意义 (``ln(1 Ω)`` 更没有)。因此
#: ``exp(-Ea/(k·T))`` 这种写法在物理上要求 Ea 与 k·T 同量纲 —— 引擎据此给出
#: 一个**约束**, 而不只是算出结果无量纲。
_DIMENSIONLESS_ARG_FUNCS: frozenset[str] = frozenset({"exp", "log", "ln", "log10"})


def _parse(expression: str) -> Any:
    """解析为最小 AST。非法节点在语法层就报 ExpressionError。"""
    import ast

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"表达式语法错误: {expression!r} ({exc})") from exc
    return _convert(tree.body)


def _convert(node: Any) -> Any:
    import ast

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ExpressionError(f"表达式只允许数值常量, 收到 {node.value!r}")
        return Num(value=float(node.value), dimension=DIMENSIONLESS)

    if isinstance(node, ast.Name):
        return Var(name=node.id, dimension=DIMENSIONLESS)

    if isinstance(node, ast.BinOp):
        op = type(node.op).__name__
        if op not in _BINOPS:
            raise ExpressionError(f"不允许的运算符 {op!r}")
        return BinOp(op=op, left=_convert(node.left), right=_convert(node.right))

    if isinstance(node, ast.UnaryOp):
        op = type(node.op).__name__
        if op not in _UNARYOPS:
            raise ExpressionError(
                f"不允许的一元运算符 {op!r}。只允许负号。"
                " 公式里出现 e 之类的记号应改用 π 或在变量声明里给出。"
            )
        return UnaryOp(op=op, operand=_convert(node.operand))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("函数调用必须是简单名字")
        name = node.func.id
        if name not in _FUNCS:
            raise ExpressionError(
                f"不允许的函数 {name!r}。允许: {sorted(_FUNCS)}。"
                " exp/log/sin 等对带量纲参数无定义; 若需要 exp(-t/tau),"
                " 请写成 (exp(-1)*... 形式或显式声明无量纲变量。"
            )
        if node.keywords:
            raise ExpressionError("函数调用不接受关键字参数")
        return _FuncCall(name=name, args=[_convert(a) for a in node.args])

    raise ExpressionError(
        f"表达式含不允许的语法 {type(node).__name__}。"
        " 允许: 数值、变量名、+ - * / **、一元负号、白名单函数。"
    )


@dataclass(frozen=True)
class _FuncCall:
    name: str
    args: list[Any]


def _check_ast(node: Any, variables: Mapping[str, VariableSpec], expr: str) -> None:
    """静态检查: 变量必须已声明, 幂必须是无量纲常数。"""
    if isinstance(node, Var):
        if node.name not in variables:
            raise ExpressionError(
                f"变量 {node.name!r} 未声明。表达式 {expr!r} 中出现的每个变量"
                " 都必须在变量声明里给出量纲 —— 量纲是外部事实, 不能从公式反推。"
            )
        return

    if isinstance(node, Num):
        return

    if isinstance(node, BinOp):
        _check_ast(node.left, variables, expr)
        _check_ast(node.right, variables, expr)
        if node.op == "Pow":
            _check_exponent(node.right, expr)
        return

    if isinstance(node, UnaryOp):
        _check_ast(node.operand, variables, expr)
        return

    if isinstance(node, _FuncCall):
        for arg in node.args:
            _check_ast(arg, variables, expr)
        if node.name == "integral":
            # ∫ f dx: 两个实参。被积函数与积分变量都要检查 (上面已查声明)。
            if len(node.args) != 2:
                raise ExpressionError(
                    f"integral() 需要 2 个实参 (被积函数, 积分变量), 表达式 "
                    f"{expr!r} 中收到 {len(node.args)} 个"
                )
            return
        if node.name in _DIMENSIONLESS_ARG_FUNCS:
            # 个数检查放这里而不是只放在求值器里: ``check_expression`` 不求值,
            # 只在 _eval_ast 里查的话 ``exp(x, y)`` 能过校验, 直到某次求值才炸
            # —— 而那时它已经在库里了。报错也要指向真实原因 (个数), 不是
            # 「实参必须无量纲」那种把人引向别处的说法。
            if len(node.args) != 1:
                raise ExpressionError(
                    f"{node.name}() 需要 1 个实参, 表达式 {expr!r} 中收到 "
                    f"{len(node.args)} 个"
                )
            for arg in node.args:
                d = _dimension_of(arg, variables)
                if not d.is_dimensionless:
                    raise ExpressionError(
                        f"{node.name}() 的实参必须无量纲, 表达式 {expr!r} 中收到 "
                        f"{d.describe()}。exp/log 的定义域是无量纲数 —— 这不是建模"
                        f"选择而是数学事实, 所以这里报错, 而不是给一个「看起来对」"
                        f"的答案。"
                    )
            return
        return

    raise ExpressionError(f"表达式结构无法检查: {expr!r}")


def _check_exponent(node: Any, expr: str) -> None:
    """幂运算的指数必须是无量纲数。

    dim(x**n) = n * dim(x)。n 必须是数而非带单位的量, 否则结果的量纲
    不唯一。物理公式中的幂几乎总是无量纲的 (平方、立方), 因此这条
    限制在实践中不排除任何真实公式。
    """
    if not isinstance(node, Num):
        raise ExpressionError(
            f"幂运算的指数必须是无量纲数值, 表达式 {expr!r} 中收到 "
            f"{type(node).__name__}。"
            " dim(x**n) = n*dim(x), n 带单位则结果量纲不唯一。"
        )


def _dimension_of(node: Any, variables: Mapping[str, VariableSpec]) -> Dimension:
    """推导子表达式的量纲。"""
    if isinstance(node, Num):
        return node.dimension

    if isinstance(node, Var):
        return variables[node.name].dimension

    if isinstance(node, UnaryOp):
        return _dimension_of(node.operand, variables)

    if isinstance(node, BinOp):
        left = _dimension_of(node.left, variables)
        right = _dimension_of(node.right, variables)
        if node.op in ("Add", "Sub"):
            return _require_same(node, left, right)
        if node.op == "Mult":
            return left + right
        if node.op == "Div":
            return left - right
        # Pow: dim(x**n) = n * dim(x)。指数已由 _check_exponent 保证是
        # 无量纲数值, 所以量纲按整数次幂缩放。**这里必须缩放而不是返回
        # 无量纲** —— 返回无量纲会让 U**2/R 变成 1/ohm, 于是「功率公式
        # P = U²/R」被判成量纲错, 而它是齐次的。这是量纲引擎最容易写错
        # 的一处: pow 不像 mul 那样量纲相加, 而是按指数缩放。
        assert isinstance(node.right, Num)  # _check_ast 已保证
        return left * node.right.value

    if isinstance(node, _FuncCall):
        if node.name == "sqrt":
            # sqrt 就是 x**0.5, 与 Pow 分支同一条路径。
            # 不做特殊取负: 取负会得到「倒电阻」, 于是 sqrt(R)*sqrt(R)
            # 被判成不是电阻 —— 而它就是。
            base = _dimension_of(node.args[0], variables)
            return base * 0.5
        if node.name in _DIMENSIONLESS_ARG_FUNCS:
            # 实参的无量纲性已由 _check_ast 校验过, 这里只管返回值
            return DIMENSIONLESS
        if node.name == "integral":
            # ∫ f dx 的量纲是 dim f · dim x, 即两个实参量纲**相加**。
            #
            # 这一条让 I²t = ∫ i² dt (焦耳)、磁通 (Wb·s) 这类跨附录公式可以
            # 参与齐次性判定。引擎**不求值**积分 (见 _eval_ast), 但量纲可算 ——
            # W1 只需要闭合, 不需要算出来。
            return _dimension_of(node.args[0], variables) + _dimension_of(node.args[1], variables)
        # abs/min/max 恒等映射: 它们不改变量纲
        return _dimension_of(node.args[0], variables)

    raise ExpressionError(f"无法推导量纲: {type(node).__name__}")


def _require_same(node: Any, left: Dimension, right: Dimension) -> Dimension:
    """加减法两侧必须同量纲。"""
    if not left.is_compatible_with(right):
        raise DimensionError(
            f"加减法两侧量纲不齐: {left.describe()} 与 {right.describe()}。"
            " 加减只能同量纲项; 相容性放宽会让 U+I 这类无物理意义的表达式通过。"
        )
    return left


# ---- 数值求值 ----


def evaluate(expression: str, values: Mapping[str, "float | tuple[float, str]"]) -> float:
    """在给定取值下求值。

    :param values: 变量名 -> 数值。数值可以是裸 float, 或 ``(值, 单位)``
        元组 (如 ``(48, "V")``)。

        裸 float 视为**无量纲**。这不是「自动按声明的单位解释」—— 本函数
        不接收变量声明, 无从得知单位。给了裸 float 却期望它带单位, 会让
        ``1 mV`` 与 ``1 V`` 在数值上无法区分, 而结果恰好相差 1000 倍。

        带单位的元组会被换算到基本单位, 见 to_base_units。
    :raises ExpressionError: 未知变量、单位无法解析、语法错
    :raises DimensionError: 运算中出现量纲冲突 (如 U + I)
    """
    ast = _parse(expression)
    specs: dict[str, VariableSpec] = {}
    magnitudes: dict[str, float] = {}

    for name, raw in values.items():
        if isinstance(raw, tuple):
            magnitude, unit = raw
            dimension = unit_dimension(unit)
            specs[name] = VariableSpec(name=name, dimension=dimension, unit=unit)
            # 前缀换算交给 pint, 不在本文件手写 10**3。
            # 手写换算表正是 §18.10 注 1 说的那类硬编码: 新增一个前缀
            # 就得记得同步改表, 而漏改的表现是「1000 mA 算成 1000 A」——
            # 数值上完全可能落在合理区间, 人工抽检发现不了。
            magnitudes[name] = to_base_units(magnitude, unit)
        else:
            specs[name] = VariableSpec(name=name, dimension=DIMENSIONLESS, unit=None)
            magnitudes[name] = float(raw)

    _check_ast(ast, specs, expression)
    # 求值前先过一遍量纲。理由: 允许「U + I」在求值阶段算出数字会让
    # DimensionError 成为可选路径 —— 调用方若不预校验, 就会拿到一个
    # 数值上可能合理的错结果 (48 + 10 = 58), 而 58 伏安不是任何物理量。
    _dimension_of(ast, specs)
    result = _eval_ast(ast, specs, magnitudes)
    return result


def _require_arity(node: _FuncCall, expected: int) -> None:
    """检查函数实参个数。

    不检查的话 ``sqrt()`` 会 IndexError、``min(a)`` 会静默返回 a ——
    后者是错值而非异常, 比崩掉更难发现。
    """
    actual = len(node.args)
    if actual != expected:
        raise ExpressionError(
            f"{node.name} 需要 {expected} 个参数, 收到 {actual} 个。"
            f" min/max 至少两个才构成比较, 传一个会被当成恒等函数 —— 那会"
            " 让漏写参数的形式静默通过。"
        )


def _eval_ast(
    node: Any,
    variables: Mapping[str, VariableSpec],
    magnitudes: Mapping[str, float],
) -> float:
    if isinstance(node, Num):
        return node.value

    if isinstance(node, Var):
        return magnitudes[node.name]

    if isinstance(node, UnaryOp):
        return -_eval_ast(node.operand, variables, magnitudes)

    if isinstance(node, BinOp):
        left = _eval_ast(node.left, variables, magnitudes)
        right = _eval_ast(node.right, variables, magnitudes)
        if node.op == "Add":
            return left + right
        if node.op == "Sub":
            return left - right
        if node.op == "Mult":
            return left * right
        if node.op == "Div":
            if right == 0:
                raise ExpressionError("除零")
            return left / right
        # 用 math.pow 而非 ``left ** right``: 后者在指数为负且底数接近 0 时
        # 返回 inf 而不报错, 乘法器会把 inf 当成合法结果往下传。
        # math.pow 对负底数与任意实指数返回 ValueError, 能在求值处拦下。
        try:
            return math.pow(left, right)
        except (ValueError, OverflowError) as exc:
            raise ExpressionError(
                f"幂运算失败: {left} ** {right} ({exc})。"
                " 指数为无量纲数已由 _check_exponent 保证, 这里失败通常意味着"
                " 底数为负且指数非整数 —— 该结果无实数解。"
            ) from exc

    if isinstance(node, _FuncCall):
        args = [_eval_ast(a, variables, magnitudes) for a in node.args]
        if node.name == "sqrt":
            _require_arity(node, 1)
            return math.sqrt(args[0])
        if node.name == "abs":
            _require_arity(node, 1)
            return abs(args[0])
        if node.name == "min":
            _require_arity(node, 2)
            return min(args)
        if node.name == "max":
            _require_arity(node, 2)
            return max(args)
        if node.name in _DIMENSIONLESS_ARG_FUNCS:
            if len(args) != 1:
                raise ExpressionError(f"{node.name} 需要 1 个参数, 收到 {len(args)} 个")
            if node.name == "exp":
                try:
                    return math.exp(args[0])
                except OverflowError as exc:
                    raise ExpressionError(f"exp({args[0]}) 溢出: {exc}") from exc
            if node.name in ("log", "ln"):
                if args[0] <= 0:
                    raise ExpressionError(
                        f"{node.name}({args[0]}) 无实数值 —— 定义域是正数。"
                        " 返回 NaN 会被下游当成合法结果传下去。"
                    )
                return math.log(args[0])
            return math.log10(args[0])
        if node.name == "integral":
            # **量纲可算, 但数值不可算。** W1 的门禁只要求齐次性闭合, 所以
            # 上面 _dimension_of 已经给出了 ∫ f dx 的量纲。这里必须显式拒绝求值
            # —— 悄悄返回 0 或抛一个含糊的异常, 都会让「算不出来」看起来像
            # 「算出来是 0」。
            raise ExpressionError(
                "积分不做数值求值: 引擎能校验 ∫ f dx 的量纲 (dim f · dim x), "
                "但不解析积分上下限, 因此无法给出数值。需要数值时走符号求解层。"
            )
        raise ExpressionError(f"未实现的函数: {node.name}")

    raise ExpressionError(f"无法求值: {type(node).__name__}")


def explain(result: DimensionCheckResult) -> str:
    """把校验结果渲染成一行说明, 用于导入报告。"""
    status = "OK" if result.dimension_ok else "FAIL"
    text = f"[{status}] {result.formula_id}: {result.dimension.describe()}"
    if result.reason:
        text += f" — {result.reason}"
    return text
