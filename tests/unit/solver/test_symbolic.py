"""solver.symbolic 的单元测试。

覆盖 ADR-015 的核心主张: 量纲校验能区分「算得出数」与「量纲正确」。

关键的负向用例 (P=U*I 与 P=U*I*R 都算得出数, 后者量纲错) 来自
旧 inference/engine.py 的 eval() 路径无法拦截的情形。
"""

from __future__ import annotations

import pytest

from aterag.solver.symbolic import (
    DIMENSION_COMPONENTS,
    DIMENSIONLESS,
    Dimension,
    DimensionError,
    ExpressionError,
    SymbolicError,
    VariableSpec,
    assert_dimension_ok,
    check_expression,
    evaluate,
    explain,
    unit_dimension,
    unit_registry,
)

# 常用量纲快捷方式
V = unit_dimension("V")
A = unit_dimension("A")
W = unit_dimension("W")
OHM = unit_dimension("ohm")
F = unit_dimension("F")
H = unit_dimension("H")
HZ = unit_dimension("Hz")
S = unit_dimension("s")
J = unit_dimension("J")


class TestDimensionAlgebra:
    def test_seven_components_in_declared_order(self) -> None:
        assert DIMENSION_COMPONENTS == (
            "length",
            "mass",
            "time",
            "current",
            "temperature",
            "amount",
            "luminous_intensity",
        )

    def test_from_tuple_roundtrip(self) -> None:
        d = Dimension(1, 2, 3, 4, 5, 6, 7)
        assert d.as_tuple() == (1, 2, 3, 4, 5, 6, 7)
        assert Dimension.from_tuple((1, 2, 3, 4, 5, 6, 7)) == d

    def test_from_tuple_rejects_wrong_arity(self) -> None:
        with pytest.raises(SymbolicError, match="必须是 7 维"):
            Dimension.from_tuple((1, 2, 3))

    def test_multiply_add(self) -> None:
        """电压 [L²M T⁻³I⁻¹] + 电流 [I] = 功率 [L²M T⁻³]。"""
        assert (V + A).is_compatible_with(W)

    def test_subtraction_requires_same_dimension(self) -> None:
        """W - V 不是 W, W - W 也不是 W。

        功率减电压没有物理意义, 所以相容性判定是严格相等而非「量纲可换算」。
        这条断言锁住该语义: 若哪天把 is_compatible_with 放宽成「能换算就行」,
        ``P - U`` 这类式子会被放行, 而它的值在数值上完全可能落在合理区间。

        W - W = 无量纲 (功率差仍���功率的两倍系数, 单位抵消), 这一点值得
        单独断言 —— 它是「同量纲相减后仍同量纲」的推广。
        """
        assert (W - V).is_compatible_with(W) is False
        assert (W - W).is_dimensionless

    def test_divide_is_subtract(self) -> None:
        assert (V - A).is_compatible_with(OHM)

    def test_negate_is_self_inverse(self) -> None:
        """取负两次回到原量纲。

        用于覆盖 __neg__ 与 __mul__(-1) 的组合 —— 若 __neg__ 实现错成
        「转置」之类, 这个断言会先发现。
        """
        assert (-(-V)).is_compatible_with(V)

    def test_dimensionless_default(self) -> None:
        assert Dimension().is_dimensionless
        assert Dimension(0, 0, 0, 0, 0, 0, 0).is_dimensionless

    def test_is_frozen(self) -> None:
        with pytest.raises(Exception):
            Dimension().length = 1  # type: ignore[misc]

    def test_describe_dimensionless(self) -> None:
        assert DIMENSIONLESS.describe() == "无量纲 [1]"

    def test_describe_single_exponent(self) -> None:
        assert unit_dimension("Hz").describe() == "时间^-1"

    def test_describe_multiple_components(self) -> None:
        text = unit_dimension("J").describe()
        assert "长度" in text
        assert "质量" in text
        assert "时间" in text

    def test_is_compatible_is_strict_equality(self) -> None:
        """相容 == 严格相等, 不是「单位可换算」。

        允许「相容但不等」会把 U+I 放行 —— 伏特加安培没有物理意义。
        """
        assert V.is_compatible_with(unit_dimension("volt")) is True
        assert V.is_compatible_with(W) is False


class TestUnitDimension:
    def test_voltage(self) -> None:
        # V = kg*m^2/(s^3*A) = [L^2 M T^-3 I^-1]
        # 注意是 L^2 不是 L^1 —— 伏特由「力/电荷」导出, 力含长度一次方,
        # 电荷含电流一次方与时间一次方, 两者相除再乘时间得 L^2。
        # 写错这一项不会让任何公式 «看起来不对», 只会让齐次性判定在
        # 电压参与的式子里全部失真, 所以这里逐项断言。
        assert V.as_tuple() == (2, 1, -3, -1, 0, 0, 0)

    def test_current(self) -> None:
        assert A.as_tuple() == (0, 0, 0, 1, 0, 0, 0)

    def test_power_is_v_times_a(self) -> None:
        # W = kg*m^2/s^3 = [L^2 M T^-3]
        assert W.as_tuple() == (2, 1, -3, 0, 0, 0, 0)

    def test_resistance(self) -> None:
        # ohm = V/A = [L^2 M T^-3 I^-2]
        # 电流分量是 -2 而不是 -1 —— 阻抗是「每安培的伏特数」, 比电压
        # 多除一个电流。写成 -1 会让 U*I/R 的量纲算成瓦特/安培, 于是
        # 最基本的功率除以电阻公式反而被判不齐。
        assert OHM.as_tuple() == (2, 1, -3, -2, 0, 0, 0)

    def test_farad_is_coulomb_per_volt(self) -> None:
        # F = A*s/V = [L^-2 M^-1 T^4 I^2]
        assert F.as_tuple() == (-2, -1, 4, 2, 0, 0, 0)

    def test_henry(self) -> None:
        # H = V*s/A = [L^2 M T^-2 I^-2]
        assert H.as_tuple() == (2, 1, -2, -2, 0, 0, 0)

    def test_hertz(self) -> None:
        assert HZ.as_tuple() == (0, 0, -1, 0, 0, 0, 0)

    def test_joule_is_watt_second(self) -> None:
        assert J.as_tuple() == (2, 1, -2, 0, 0, 0, 0)

    def test_engineering_prefixes_collapse(self) -> None:
        """uF 与 F 量纲相同。

        §6.2 的术语表大量使用工程前缀。缺了别名的话同一个物理量会因写法
        不同被判成不同量纲。
        """
        assert unit_dimension("uF").is_compatible_with(F)
        assert unit_dimension("nF").is_compatible_with(F)
        assert unit_dimension("mH").is_compatible_with(H)
        assert unit_dimension("kHz").is_compatible_with(HZ)
        assert unit_dimension("MHz").is_compatible_with(HZ)

    def test_ratio_expression(self) -> None:
        assert unit_dimension("V/A").is_compatible_with(OHM)
        assert unit_dimension("1/s").is_compatible_with(HZ)

    def test_switch_and_cycle_are_dimensionless(self) -> None:
        assert unit_dimension("switch").is_dimensionless

    def test_percent_is_dimensionless(self) -> None:
        """百分比的量纲是 0.01 (无量纲)。

        这不是「无量纲」的同义词, 但在齐次性判定里等价 ——
        两侧都是百分比时相容, 一侧百分比一侧伏特则不相容。
        """
        assert unit_dimension("percent").is_dimensionless

    def test_rejects_unknown_unit(self) -> None:
        with pytest.raises(ExpressionError, match="单位无法解析"):
            unit_dimension("zork")

    def test_accepts_pint_beyond_our_7_dims(self) -> None:
        """pint 认得的、落在 SI 基本量空间内的单位都要接受。

        坎德拉与开尔文是 SI 的七个基本量之二。若拒绝它们, 等于说
        「本系统认为坎德拉不是物理量」—— 那是把 SI 体系的一个基本量
        误判成未知单位。

        走一遍 candela 与 lux 的映射, 覆盖 _PINT_BASE_ALIASES 的
        luminosity 分支。
        """
        cd = unit_dimension("candela")
        assert cd.as_tuple()[6] == 1
        # lux = lumen/m^2 = cd/m^2
        lux = unit_dimension("lux")
        assert lux.as_tuple()[6] == 1
        assert lux.as_tuple()[0] == -2

    def test_rejects_dimension_outside_seven_dims(self) -> None:
        """超出 SI 七个基本量的因子必须报错而非忽略。

        pint 支持自定义基本量 (define 一个新的 [widget]), 那种量纲在本
        系统里无法映射成 7 维向量。必须报错 —— 忽略会把「未知量纲」当成
        无量纲, 于是 E = m*c^2 里的 c 被当成裸数, 公式「齐次」通过而实际
        不成立。

        直接喂一个假 dimensionality 而不是在全局注册表里 define 临时单位:
        改全局注册表会污染同进程内后续所有测试的量纲判定, 且 pint 没有
        公开的注销 API (清理只能碰 ``_units`` 私有字典, 那会连带销毁
        整个注册表)。用假 dimensionality 覆盖同一条代码路径, 且无副作用。
        """
        from aterag.solver.symbolic import _pint_dim_to_dimension

        with pytest.raises(ExpressionError, match="7 维之外"):
            _pint_dim_to_dimension({"[widget]": 1}, "1 widget")

    def test_rejects_mixed_leftover_with_known_dims(self) -> None:
        """已识别分量与未知分量并存时, 仍必须整体拒绝。

        不能因为「认得其中一部分」就放过 —— 那会算出一个缺项的 7 维向量,
        而缺项位置的 0 会被读成「该量为零次方」, 也就是无量纲。
        """
        from aterag.solver.symbolic import _pint_dim_to_dimension

        with pytest.raises(ExpressionError, match="7 维之外"):
            _pint_dim_to_dimension({"[length]": 1, "[widget]": 2}, "1 m*widget^2")

    def test_registry_is_singleton(self) -> None:
        assert unit_registry() is unit_registry()


def specs(**kwargs: tuple[str, str]) -> dict[str, VariableSpec]:
    """便捷构造: ``specs(U="V", I="A")`` -> {U: VariableSpec(...)}"""
    return {
        name: VariableSpec(name=name, dimension=unit_dimension(unit))
        for name, unit in kwargs.items()
    }


class TestHomogeneityPositive:
    def test_ohms_law(self) -> None:
        r = check_expression("U*I", specs(U="V", I="A"), lhs_dimension=W)
        assert r.dimension_ok
        assert r.dimension.is_compatible_with(W)

    def test_ohms_law_resistance_form(self) -> None:
        r = check_expression("U**2/R", specs(U="V", R="ohm"), lhs_dimension=W)
        assert r.dimension_ok

    def test_ripple_current(self) -> None:
        """ΔI_L = (V_on * D) / (L * f_sw)  —— 附录W 的电感纹波公式。"""
        r = check_expression(
            "V*D/(L*f)", specs(V="V", D="dimensionless", L="H", f="Hz"), lhs_dimension=A
        )
        assert r.dimension_ok

    def test_rc_time_constant(self) -> None:
        """tau = R*C -> 秒。"""
        r = check_expression("R*C", specs(R="ohm", C="F"), lhs_dimension=S)
        assert r.dimension_ok

    def test_sum_of_same_dimension(self) -> None:
        """U*I + U*U/R: 两项都是瓦特。"""
        r = check_expression("U*I + U**2/R", specs(U="V", I="A", R="ohm"), lhs_dimension=W)
        assert r.dimension_ok

    def test_sqrt_halves_exponent(self) -> None:
        """sqrt(R): 量纲整体乘 0.5, **不是**取负。

        电阻 [L²M T⁻³I⁻²] 折半 -> [L M^0.5 T^-1.5 I^-1]。这是对数坐标下
        的几何均值阻抗, 真实用途。

        若实现成取负, sqrt(R) 会得到「倒电阻」的量纲, 于是
        ``sqrt(R) * sqrt(R)`` 不再等于 R —— 而它显然应该等于。
        这条断言锁住「折半」而非「取负」。
        """
        r = check_expression("sqrt(R)", specs(R="ohm"))
        assert r.dimension.as_tuple() == (1.0, 0.5, -1.5, -1.0, 0.0, 0.0, 0.0)
        # 关键性质: sqrt(R)*sqrt(R) 的量纲必须回到 R
        squared = check_expression("sqrt(R)*sqrt(R)", specs(R="ohm"))
        assert squared.dimension.is_compatible_with(OHM)
        assert squared.dimension.has_fractional_parts is False

    def test_sqrt_of_power_has_fractional_dimension(self) -> None:
        """sqrt(P) 的量纲分量是半整数。

        功率 [L²M T⁻³] 折半 -> [L M^0.5 T^-1.5]。这样的量纲没有对应的
        命名单位, 但它是对的 —— 几何平均量本来就是实数量。

        显式断言 has_fractional_parts, 让「这类公式需要人工复核」这件事
        在代码里可见, 而不是靠人眼发现分母上有小数。
        """
        r = check_expression("sqrt(P)", specs(P="W"))
        assert r.dimension.length == 1
        assert r.dimension.mass == 0.5
        assert r.dimension.time == -1.5
        assert r.dimension.has_fractional_parts is True

    def test_integer_dimension_has_no_fractional_parts(self) -> None:
        r = check_expression("U*I", specs(U="V", I="A"))
        assert r.dimension.has_fractional_parts is False

    def test_sqrt_of_dimensionless_stays_dimensionless(self) -> None:
        r = check_expression("sqrt(d)", specs(d="dimensionless"))
        assert r.dimension.is_dimensionless

    def test_explicit_dimensionless_factor(self) -> None:
        """duty = 0.5 作为无量纲数参与, 量纲不被污染。"""
        r = check_expression("V*d", specs(V="V", d="dimensionless"), lhs_dimension=V)
        assert r.dimension_ok

    def test_nested_parentheses(self) -> None:
        """U*(U + I*R)/R: 括内两项都是电压。

        I*R = 安培×欧姆 = 伏特, 与外层的 U 同量纲, 因此括内相加合法。
        这正是齐次性判定的可用场景: 括号内各项量纲相同才允许合并,
        而「相同」正是靠它们各自的推导得出。
        """
        # U*(U + I*R)/R = U*(U + U)/R = 2U^2/R —— 功率
        r = check_expression("U*(U + I*R)/R", specs(U="V", I="A", R="ohm"), lhs_dimension=W)
        assert r.dimension_ok

    def test_nested_sum_rejects_mixed_inner_terms(self) -> None:
        """括内混进一个电流项时, 在最内层就报出来。

        括号内 (U + I) 分别是伏特与安培 —— 错误在最近的那一层暴露,
        而不是让整条公式算出一个数。这依赖加法子树的量纲先被推导。
        """
        with pytest.raises(DimensionError, match="加减法两侧量纲不齐"):
            check_expression("U*(U + I)/R", specs(U="V", I="A", R="ohm"))


class TestHomogeneityNegative:
    def test_power_times_resistance_rejected(self) -> None:
        """P = U*I*R —— eval() 会算出数, 但量纲是瓦特×欧姆。

        这是 ADR-015 的核心用例: 旧路径拦不住它。
        """
        r = check_expression("U*I*R", specs(U="V", I="A", R="ohm"), lhs_dimension=W)
        assert not r.dimension_ok
        assert "不一致" in (r.reason or "")

    def test_v_plus_i_rejected(self) -> None:
        """U + I: 伏特加安培。"""
        with pytest.raises(DimensionError, match="加减法两侧量纲不齐"):
            check_expression("U + I", specs(U="V", I="A"))

    def test_mismatched_sum_inside_larger_expression(self) -> None:
        with pytest.raises(DimensionError, match="加减法两侧量纲不齐"):
            check_expression("U*I + U*I*R", specs(U="V", I="A", R="ohm"))

    def test_assert_raises(self) -> None:
        with pytest.raises(DimensionError, match="量纲不齐"):
            assert_dimension_ok("U*I*R", specs(U="V", I="A", R="ohm"), lhs_dimension=W)

    def test_assert_message_cites_gate_g1(self) -> None:
        with pytest.raises(DimensionError, match="G1"):
            assert_dimension_ok("U*I*R", specs(U="V", I="A", R="ohm"), lhs_dimension=W)

    def test_dimension_vec_is_numeric_list(self) -> None:
        """dimension_vec() 返回 7 个浮点, 直接可写 NUMERIC(8,4)[]。

        顺序即 Dimension.as_tuple() 的顺序, 必须与 schema_full.sql 里
        dimension_vec 的下标语义一致 (SI 基本量长,质量,时间,电流,
        温度,物质的量,发光强度)。
        """
        r = check_expression("U*I", specs(U="V", I="A"), lhs_dimension=W)
        vec = r.dimension_vec()
        assert len(vec) == 7
        assert all(isinstance(v, float) for v in vec)
        # W = L^2 M T^-3
        assert vec == [2.0, 1.0, -3.0, 0.0, 0.0, 0.0, 0.0]

    def test_dimension_vec_roundtrips_through_tuple(self) -> None:
        """vec -> Dimension.from_tuple 应还原出同一个量纲。

        这条保证 dimension_vec 落库后能无损还原。若 from_tuple 丢了
        分量或改序, 数据库里的向量就会与内存中的判定依据不一致。
        """
        r = check_expression("U*I", specs(U="V", I="A"), lhs_dimension=W)
        restored = Dimension.from_tuple(tuple(int(v) for v in r.dimension_vec()))
        assert restored == r.dimension

    def test_reason_is_populated_on_failure_only(self) -> None:
        ok = check_expression("U*I", specs(U="V", I="A"), lhs_dimension=W)
        assert ok.reason is None
        bad = check_expression("U*I*R", specs(U="V", I="A", R="ohm"), lhs_dimension=W)
        assert bad.reason


class TestExpressionSafety:
    def test_rejects_unknown_variable(self) -> None:
        with pytest.raises(ExpressionError, match="未声明"):
            check_expression("U*I", specs(U="V"))

    def test_rejects_attribute_access(self) -> None:
        with pytest.raises(ExpressionError, match="不允许的语法"):
            check_expression("math.pi", {})

    def test_rejects_lambda(self) -> None:
        with pytest.raises(ExpressionError, match="不允许的语法"):
            check_expression("lambda x: x", {})

    def test_rejects_symbolic_exponent(self) -> None:
        """U**V 里的 V 虽是已声明变量, 但作指数不合法。

        指数必须是无量纲数值: dim(x**n) = n*dim(x), n 带单位则结果量纲
        不唯一。物理公式中的幂几乎总是无量纲的 (平方、立方),
        所以这条限制在实践中不排除任何真实公式。
        """
        with pytest.raises(ExpressionError, match="指数必须是无量纲数值"):
            check_expression("U**V", specs(U="V", V="V"))

    def test_fractional_exponent_is_allowed(self) -> None:
        """U**0.5 合法, 且量纲折半。

        与 sqrt 同一路径。这条与「指数必须是无量纲数值」不矛盾: 前者限制
        的是**指数的量纲**, 后者限制的是**指数是否带单位**。0.5 无量纲。
        """
        r = check_expression("U**0.5", specs(U="V"))
        assert r.dimension.length == 1
        assert r.dimension.has_fractional_parts is True

    def test_zero_exponent_is_dimensionless(self) -> None:
        """任何量的 0 次幂是无量纲。"""
        r = check_expression("U**0", specs(U="V"))
        assert r.dimension.is_dimensionless

    def test_rejects_comparison(self) -> None:
        with pytest.raises(ExpressionError, match="不允许的语法"):
            check_expression("U > 1", specs(U="V"))

    def test_rejects_name_call(self) -> None:
        with pytest.raises(ExpressionError, match="函数调用必须是简单名字"):
            check_expression("f()()", {})

    def test_rejects_string_literal(self) -> None:
        with pytest.raises(ExpressionError, match="只允许数值常量"):
            check_expression("'abc'", {})

    def test_rejects_bool_literal(self) -> None:
        """True/False 必须被拒。

        它们是 int 的子类, ``isinstance(True, int)`` 为真。若只按 int 判,
        布尔会静默变成 1/0 参与运算 —— ``U*True`` 会算出与 ``U*1`` 相同的
        功率, 而公式作者本意可能是「开关量」。类型退化成数值比报错危险。
        """
        with pytest.raises(ExpressionError, match="只允许数值常量"):
            check_expression("True", {})

    def test_rejects_comparison_operator(self) -> None:
        with pytest.raises(ExpressionError, match="不允许的语法"):
            check_expression("U > 1", specs(U="V"))

    def test_rejects_subscript(self) -> None:
        with pytest.raises(ExpressionError, match="不允许的语法"):
            check_expression("a[0]", specs(a="V"))

    def test_rejects_conditional_expression(self) -> None:
        """a if b else c —— 允许它等于允许了隐式类型分支。

        电源公式里不应出现条件表达式; 出现即说明这是代码而不是公式,
        应改用 min/max 或拆成两条公式。
        """
        with pytest.raises(ExpressionError, match="不允许的语法"):
            check_expression("U if I else R", specs(U="V", I="A", R="ohm"))

    def test_rejects_dict_display(self) -> None:
        with pytest.raises(ExpressionError, match="不允许的语法"):
            check_expression("{}", {})

    def test_rejects_tuple_display(self) -> None:
        with pytest.raises(ExpressionError, match="不允许的语法"):
            check_expression("(1, 2)", {})

    def test_rejects_exp_of_a_dimensional_argument(self) -> None:
        """``exp`` 现在**允许**了, 但实参必须无量纲 —— 约束没放松, 反而更紧。

        早先一版是把 ``exp`` 整条挡在白名单外, 那连 ``exp(-t/tau)`` 这种
        正确的写法一起挡掉了。现在放行并**改为校验实参**: ``exp(1 V)`` 无意义,
        而 ``exp(-Ea/(k_B*T))`` 里 Ea 与 k_B·T 同量纲这件事, 引擎现在能
        主动查出来。
        """
        with pytest.raises(ExpressionError, match="实参必须无量纲"):
            check_expression("exp(-Ea/T)", specs(Ea="eV", T="K"))

    def test_exp_of_a_dimensionless_ratio_is_accepted(self) -> None:
        """``exp(-t/tau)`` 是电源公式里最常见的指数项, 必须能校验。"""
        result = check_expression("exp(-t/tau)", specs(t="s", tau="s"))
        assert result.dimension_ok, result.reason
        assert result.dimension.is_dimensionless

    def test_exp_arity_is_checked(self) -> None:
        with pytest.raises(ExpressionError, match="需要 1 个实参"):
            check_expression("exp(t, tau)", specs(t="s", tau="s"))

    def test_rejects_log_of_a_dimensional_argument(self) -> None:
        """``ln(1 Ω)`` 没有实数值, 更没有量纲意义。"""
        with pytest.raises(ExpressionError, match="实参必须无量纲"):
            check_expression("log(U)", specs(U="V"))

    def test_rejects_keyword_arguments(self) -> None:
        with pytest.raises(ExpressionError, match="关键字参数"):
            check_expression("abs(x=-1)", specs(x="dimensionless"))

    def test_rejects_unary_plus(self) -> None:
        with pytest.raises(ExpressionError, match="一元运算符"):
            check_expression("+U", specs(U="V"))

    def test_rejects_syntax_error(self) -> None:
        with pytest.raises(ExpressionError, match="语法错误"):
            check_expression("U * * I", specs(U="V", I="A"))

    def test_exponent_must_be_dimensionless_number(self) -> None:
        with pytest.raises(ExpressionError, match="指数必须是无量纲数值"):
            check_expression("U**x", specs(U="V", x="dimensionless"))

    def test_safety_vs_eval_contrast(self) -> None:
        """对照: 这些都是 eval() 能算但必须被拦的。

        旧 inference/engine.py:106 用 eval() + 正则黑名单, 对以下全部放行。
        """
        for bad in ["__import__('os').system('x')", "open('/etc/passwd')"]:
            with pytest.raises(ExpressionError):
                check_expression(bad, {})


class TestEvaluate:
    def test_numeric(self) -> None:
        assert evaluate("U*I", {"U": 48.0, "I": 10.0}) == pytest.approx(480.0)

    def test_with_units(self) -> None:
        assert evaluate("U*I", {"U": (48.0, "V"), "I": (10.0, "A")}) == pytest.approx(480.0)

    def test_engineering_prefix_scaled(self) -> None:
        """1000 mA 应等于 1 A。

        单位换算由 pint 完成, 不由求值器手写 10**3 —— 后者正是 §18.10
        注 1 说的那类硬编码。
        """
        assert evaluate("U*I", {"U": (48.0, "V"), "I": (1000.0, "mA")}) == pytest.approx(48.0)

    def test_division(self) -> None:
        assert evaluate("U/R", {"U": (48.0, "V"), "R": (24.0, "ohm")}) == pytest.approx(2.0)

    def test_power_operator(self) -> None:
        assert evaluate("U**2/R", {"U": (10.0, "V"), "R": (5.0, "ohm")}) == pytest.approx(20.0)

    def test_unary_minus(self) -> None:
        assert evaluate("-U", {"U": (5.0, "V")}) == pytest.approx(-5.0)

    def test_sqrt(self) -> None:
        assert evaluate("sqrt(R)", {"R": (4.0, "ohm")}) == pytest.approx(2.0)

    def test_abs_min_max(self) -> None:
        assert evaluate("abs(-x)", {"x": (3.0, "V")}) == pytest.approx(3.0)
        assert evaluate("min(a,b)", {"a": (1.0, "V"), "b": (2.0, "V")}) == pytest.approx(1.0)
        assert evaluate("max(a,b)", {"a": (1.0, "V"), "b": (2.0, "V")}) == pytest.approx(2.0)

    def test_mixed_sum_raises(self) -> None:
        """相容性校验在求值时同样生效, 不是只在校验时。"""
        with pytest.raises(DimensionError, match="加减法两侧量纲不齐"):
            evaluate("U + I", {"U": (1.0, "V"), "I": (1.0, "A")})

    def test_division_by_zero(self) -> None:
        with pytest.raises(ExpressionError, match="除零"):
            evaluate("U/R", {"U": (1.0, "V"), "R": (0.0, "ohm")})

    def test_unknown_variable(self) -> None:
        with pytest.raises(ExpressionError, match="未声明"):
            evaluate("U*I", {"U": (1.0, "V")})


class TestExplain:
    def test_ok_line(self) -> None:
        """describe() 给基本量的组合, 不是「功率」这种派生量名。

        给派生量名需要一张对照表, 而那张表是手工维护的, 且 §18.10 注 3
        警告过事后补表会产生「看起来对但量纲错」的条目。直接展开基本量
        没有这个维护面。
        """
        r = check_expression("U*I", specs(U="V", I="A"), lhs_dimension=W)
        text = explain(r)
        assert text.startswith("[OK]")
        assert "长度^2" in text
        assert "质量" in text

    def test_fail_line_includes_reason(self) -> None:
        r = check_expression("U*I*R", specs(U="V", I="A", R="ohm"), lhs_dimension=W)
        text = explain(r)
        assert text.startswith("[FAIL]")
        assert "不一致" in text


class TestResultShape:
    def test_formula_id_is_carried(self) -> None:
        r = check_expression("U*I", specs(U="V", I="A"), formula_id="F_W.1")
        assert r.formula_id == "F_W.1"

    def test_lhs_dimension_is_recorded(self) -> None:
        r = check_expression("U*I", specs(U="V", I="A"), lhs_dimension=W)
        assert r.lhs_dimension is not None
        assert r.lhs_dimension.is_compatible_with(W)

    def test_no_lhs_skips_comparison(self) -> None:
        """不给 lhs_dimension 时只报表达式量纲, 不判失败。

        用于「这条公式的量纲是多少」这类查询, 而不是门禁。
        """
        r = check_expression("U*I*R", specs(U="V", I="A", R="ohm"))
        assert r.dimension_ok

class TestIntegralIsDimensionOnly:
    """``∫ f dx`` 的量纲可算, 但数值不可算 —— 这两件事必须分开表达。

    引擎不含积分上下限的概念, 所以它**永远给不出数值**; 但
    ``dim(∫ f dx) = dim f · dim x`` 是确定的, 而 W1 的门禁只要求齐次性闭合。
    不把这两件事分开表达, 就会出现「悄悄返回 0」—— 那会让「算不出来」看起来
    像「算出来是 0」。
    """

    def test_dimension_is_integrand_times_differential(self) -> None:
        r = check_expression("integral(i*i, t)", specs(i="A", t="s"))
        # dim i^2 * dim t = A^2*s -- **NOT** joule. I^2t is a heat proxy;
        # reading it as joule is a common wrong intuition.
        assert r.dimension == Dimension(time=1.0, current=2.0)

    def test_closes_against_a_matching_lhs(self) -> None:
        sp = specs(i="A", t="s")
        r = check_expression(
            "integral(i*i, t)", sp, lhs_dimension=Dimension(time=1.0, current=2.0)
        )
        assert r.dimension_ok, r.reason

    def test_rejects_a_wrong_lhs(self) -> None:
        """把 ∫ i² dt 判成与 A 同量纲必须是错的。"""
        r = check_expression("integral(i*i, t)", specs(i="A", t="s"), lhs_dimension=unit_dimension("A"))
        assert not r.dimension_ok

    def test_numeric_evaluation_is_refused_loudly(self) -> None:
        with pytest.raises(ExpressionError, match="不做数值求值"):
            evaluate("integral(i*i, t)", {"i": 2.0, "t": 3.0})

    def test_arity_is_enforced(self) -> None:
        with pytest.raises(ExpressionError, match="需要 2 个实参"):
            check_expression("integral(i)", specs(i="A"))


class TestExpAndLogEvaluation:
    def test_exp_evaluates(self) -> None:
        sp = specs(x="dimensionless")
        r = check_expression("exp(x)", sp)
        assert r.dimension_ok, r.reason
        assert r.dimension.is_dimensionless
        assert evaluate("exp(x)", {"x": 0.0}) == pytest.approx(1.0)
        assert evaluate("exp(x)", {"x": 1.0}) == pytest.approx(2.718281828459045)

    def test_log10_evaluates(self) -> None:
        assert evaluate("log10(x)", {"x": 100.0}) == pytest.approx(2.0)

    def test_log_of_non_positive_is_refused(self) -> None:
        """返回 NaN 会被下游当成合法结果一路传下去。"""
        with pytest.raises(ExpressionError, match="定义域"):
            evaluate("log(x)", {"x": -1.0})

    def test_exp_overflow_is_refused(self) -> None:
        with pytest.raises(ExpressionError, match="溢出"):
            evaluate("exp(x)", {"x": 1e6})

    def test_exp_arity_is_checked(self) -> None:
        """实参个数错要报「个数」而不是「无量纲」—— 报错要指向真实原因。"""
        with pytest.raises(ExpressionError, match="需要 1 个实参"):
            check_expression("exp(x, y)", specs(x="dimensionless", y="dimensionless"))
