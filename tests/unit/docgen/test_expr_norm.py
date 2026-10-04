"""``docgen.expr_norm`` 的单元测试 —— 语料表达式 -> 引擎语法。

测试重心是**「返回了合法结果但数学是错的」**那几类, 不是往返一致性。
归一化的产出会直接决定每条公式的 ``dimension_vec``, 而量纲算错时不会报错,
只会算错, 所以每条替换规则都必须有一���钉住它的用例。

不需要数据库与方案文件。
"""

from __future__ import annotations

import ast

import pytest

from aterag.docgen.expr_norm import PI_LITERAL, normalize_equation


def rhs_of(raw: str) -> str:
    n = normalize_equation(raw)
    assert n.ok, f"归一化失败: {n.reason}"
    assert n.rhs is not None
    return n.rhs


class TestNoSilentWrongMath:
    """这一组每一条都对应一个真实踩过的坑。"""

    def test_pi_does_not_glue_to_a_neighbouring_digit(self) -> None:
        """``2π`` 若先折成常量会得到 ``23.14…``。

        多项式被粘成一个巨大常数, 而 ``ast`` 照样通过 —— 这是最危险的一类:
        不报错, 只是数学全错。所以 ``π`` 必须**先落成标识符占位**, 等隐含乘法
        补完再代入常量。
        """
        got = rhs_of("f_RHPZ = R_load·(1−D)·2/(2π·L)")
        assert got == f"R_load*(1-D)*2/(2*{PI_LITERAL}*L)"
        assert ast.parse(got, mode="eval")

    def test_division_does_not_get_a_multiply(self) -> None:
        """``(a)`` 后无条件补 ``*`` 会把 ``(a)/(b)`` 变成 ``(a)*/(b)``。"""
        got = rhs_of("ΔI_L = (V_on·D)/(L·f_sw)")
        assert got == "(V_on*D)/(L*f_sw)"
        assert ast.parse(got, mode="eval")

    def test_superscript_becomes_a_power_not_a_digit(self) -> None:
        """``(1−D)²`` 若只把 ``²`` 翻成 ``2``, 会得到 ``(1-D)2``。

        若恰好两边都是标识符, 它会变成 ``(1-D)x2`` —— 合法但语义完全不同的
        表达式。幂号必须补。
        """
        got = rhs_of("f_RHPZ = R_load·(1−D)²/(2π·L)")
        assert "(1-D)**2" in got

    def test_superscript_in_both_unicode_blocks(self) -> None:
        """``²`` 是 U+00B2, ``⁻¹`` 是 U+207B+U+00B9 —— 两个区, 漏一个就失效。

        只覆盖 U+207x 区时 41 条含 ``²`` 的公式全部解析失败。
        """
        assert "**2" in rhs_of("PF = cosφ·sqrt(1+THD_I²)")
        assert "**-1" in rhs_of("s = (1−z⁻¹)/T_s")

    def test_subscript_keeps_distinction(self) -> None:
        """``I₁`` 必须变 ``I_1`` 而不是 ``I`` —— 折成 ``I`` 会把 ``I₁`` 与
        ``I₂`` 合并成同一个量, 而两者量纲未必相同 (峰值 vs 有效值)。"""
        n = normalize_equation("I_total = I₁ + I₂")
        assert n.ok, n.reason
        assert "I_1" in (n.rhs or "")
        assert "I_2" in (n.rhs or "")

    def test_star_modifier_is_rejected_not_guessed(self) -> None:
        """``K*`` 是修正值, ``z*`` 是复共轭 —— 两者都不是乘号。

        丢掉星号会把 ``K*`` 当成 ``K`` (伪造恒等式); 当乘号则给 ``z*`` 凭空
        加一个星号因子。折成 ``K_star`` 更糟: 共轭与修正值混成同一个符号,
        实测污染出 10 个假变量。
        """
        for raw in ("K* = V_F/(V_F + I_o·R_ds(on))", "V_out = z*·H·ω"):
            n = normalize_equation(raw)
            assert not n.ok, raw
            assert "星号" in (n.reason or "")

    def test_star_between_operands_is_still_multiplication(self) -> None:
        """排除修饰星号时不能误伤正常乘号 —— 也不能误伤上标折叠出的 ``**``。"""
        assert rhs_of("P = U·I") == "U*I"
        # ``ω₀`` 会折成 ``ω**0``; 若不把 ``*`` 排除在「记号开头」之外,
        # ``ω**0`` 会被判成星号修饰 (实测误杀 105 条)。
        assert "ω_0" in (normalize_equation("ω₀ = 1/sqrt(LC)").lhs or "")


class TestImplicitMultiplication:
    def test_digit_then_identifier(self) -> None:
        assert rhs_of("f_r = 1/(2π·L_r·C_r)") == f"1/(2*{PI_LITERAL}*L_r*C_r)"

    def test_closing_paren_then_identifier(self) -> None:
        assert rhs_of("X = (a+b)·c") == "(a+b)*c"

    def test_no_multiply_before_operator(self) -> None:
        got = rhs_of("X = (a+b)/(c−d)")
        assert got == "(a+b)/(c-d)"


class TestFunctionAndGroupRewrites:
    def test_sqrt_of_parenthesised_group(self) -> None:
        assert rhs_of("f_0 = 1/(2π·√(L·C))") == f"1/(2*{PI_LITERAL}*sqrt(L*C))"

    def test_sqrt_of_bare_operand(self) -> None:
        assert rhs_of("X = √2·a") == "sqrt(2)*a"

    def test_sqrt_does_not_invent_a_product(self) -> None:
        """``√ab`` 分不清「a×b」与「一个叫 ab 的量」。

        补乘号就是替方案做数学决定, 所以只取单个记号。
        """
        assert rhs_of("X = √ab") == "sqrt(ab)"

    def test_exp_is_expanded_so_the_engine_decides(self) -> None:
        """``e^x`` 翻成 ``exp(x)`` 而不是留着 ``e``。

        留着 ``e`` 会让它变成一个待查量纲的变量, 于是公式以「变量 e 不在符号表」
        被排除, 报错指向符号表覆盖度而不是引擎白名单。
        """
        assert rhs_of("M_os = e^(−π·σ/ω_d)") == f"exp(-{PI_LITERAL}*σ/ω_d)"

    def test_latex_norm_becomes_abs(self) -> None:
        """``\\|X\\|`` 是模长, 而 ``abs`` 在引擎白名单里。"""
        n = normalize_equation("Z_1 ≤ \\|Z_o(f)\\|/3")
        assert n.ok, n.reason
        assert "abs(" in (n.rhs or "")

    def test_call_arguments_keep_their_commas(self) -> None:
        """下标逗号只该在括号外折成下划线。"""
        got = rhs_of("f_c = min(f_sw/10, f_RHPZ/5)")
        assert "," in got

    def test_subscript_comma_outside_parens_becomes_underscore(self) -> None:
        assert rhs_of("Δt = t_relay,op + t_reset") == "t_relay_op+t_reset"


class TestRejections:
    """拒因必须具体到能定位, 而不是一句「无法解析」。"""

    @pytest.mark.parametrize(
        ("raw", "needle"),
        [
            ("未满足的 capability 集合 → INSTRUMENT_GAP", "中文"),
            ("P_loss = ∑ P_i", "求和"),
            ("I²t = ∫ i² dt", "积分"),
            ("t_50 = tan(2)·MTTF", "超越函数"),
            ("R_ds(on) ∝ T^1.5", "正比"),
            ("ΔI_grade ≤ 10% ~ I_set", "区间"),
            ("t_resolution ≤ 1~10 ms", "区间"),
            ("K = 1/(1+2) ≥ 3", "链式"),
        ],
    )
    def test_reason_is_specific(self, raw: str, needle: str) -> None:
        n = normalize_equation(raw)
        assert not n.ok, raw
        assert needle in (n.reason or ""), f"{raw!r} 的拒因是 {n.reason!r}"

    def test_exp_reaches_the_engine_rather_than_being_prejudged(self) -> None:
        """``e^x`` 归一化成 ``exp(x)``, **由引擎**判能否接受。

        本模块不重复维护函数白名单 —— 那样白名单会有两处, 迟早不一致。
        """
        n = normalize_equation("M_os = e^(−π·σ/ω_d)·100%")
        assert n.ok, n.reason
        assert "exp(" in (n.rhs or "")

    def test_chained_relation_is_rejected_not_flattened(self) -> None:
        """``AL_LL < AL_L < x_max`` 的语义是「各项同量纲」。

        改写成等号就是伪造方程, 所以如实报不支持。
        """
        n = normalize_equation("AL_LL < AL_L < x_min < x_max")
        assert not n.ok
        assert "链式" in (n.reason or "")

    def test_inequality_alone_is_fine(self) -> None:
        """单条不等式是合法的 (门禁会比对两侧量纲), 只有**链式**才不行。"""
        n = normalize_equation("t_timeout > N·t_heartbeat")
        assert n.ok, n.reason
        assert n.relation == ">"

    def test_empty_input(self) -> None:
        assert normalize_equation(None).reason == "空表达式"
        assert normalize_equation("").reason == "空表达式"


class TestCellWithSeveralEquations:
    """一个单元格里常有多条公式, 主公式写在最前。"""

    def test_first_clean_equation_wins(self) -> None:
        n = normalize_equation("`ω_z,esr = 1/(ESR·C_out)`，`f_esr = ω_z,esr/(2π)`")
        assert n.ok, n.reason
        # 下标里的逗号折成下划线: Python 标识符不能含逗号
        assert n.equation == "ω_z_esr = 1/(ESR*C_out)"
        assert n.extra_segments > 0

    def test_prose_paragraph_around_equation(self) -> None:
        n = normalize_equation("`V_out = D·V_in`（半桥，`D_eff = 2D`）")
        assert n.ok, n.reason
        assert n.equation == "V_out = D*V_in"

    def test_trailing_note_does_not_break_it(self) -> None:
        n = normalize_equation("`K = 2·L·f_sw/R_load'`；`K>1 CCM / K<1 DCM`")
        assert n.ok, n.reason
        assert n.equation == "K = 2*L*f_sw/R_load"

    def test_specification_is_not_a_formula(self) -> None:
        """``t_resolution ≤ 1 ms`` 是**指标**, 不是公式。必须拒。"""
        n = normalize_equation("t_resolution ≤ 1 ms")
        assert not n.ok


class TestVariablesAreTraceable:
    def test_variables_survive_translation(self) -> None:
        """变量名决定量纲字典的查表键, 改名错了不报错, 只会算错量纲。"""
        n = normalize_equation("V_out = D · V_in")
        assert n.ok, n.reason
        assert set(n.variables) == {"V_out", "D", "V_in"}
        assert set(n.source_variables) == {"V_out", "D", "V_in"}

    def test_no_constant_leaks_into_variables(self) -> None:
        """``π`` 折成常量后不得作为变量残留 —— 残留即表示代入失败。"""
        n = normalize_equation("X = 2π·L")
        assert n.ok, n.reason
        assert not [v for v in n.variables if "PIconst" in v or v == "pi"]

    def test_greek_letters_are_ordinary_identifiers(self) -> None:
        """``ΔI_L``/``τ``/``ω`` 都是合法 Python 标识符, 不该被拆。"""
        n = normalize_equation("ΔI_L = V_in·D/(L·f_sw)")
        assert n.ok, n.reason
        assert "ΔI_L" in n.variables

    def test_caret_is_power_not_bitwise_xor(self) -> None:
        """``^`` 必须翻成 ``**``。

        ``^`` 在 Python 里是**按位异或**。语料拿它写幂(Steinmetz
        ``f^α``、Weibull ``exp(λt/η)^β``)。不翻的话 ``ast`` **照样通过**,
        字母上标被当成独立物理量变量, 量纲按乘法算而不是按幂算 ——
        静默算错, 且报错都指不到真因。
        """
        for raw in ("P = k_h·f^α·B^β", "R = exp(λt/η)^β", "X = f^2", "Y = 10^3"):
            tree = ast.parse(rhs_of(raw), mode="eval")
            assert not any(
                isinstance(node, ast.BitXor) for node in ast.walk(tree)
            ), f"{raw!r} 仍含按位异或: {ast.dump(tree)}"

    def test_caret_exponent_becomes_pow_node(self) -> None:
        """``f^α`` 的根节点必须是 ``Pow``, 且指数是符号 ``α``。

        变量集合区分不了 ``f*α`` 与 ``f**α`` —— 两者都只引用 ``{f, α}``。
        所以这里断言 **AST 形状**, 而不是变量集合。
        """
        tree = ast.parse(rhs_of("P = f^α"), mode="eval")
        assert isinstance(tree.body, ast.BinOp)
        assert isinstance(tree.body.op, ast.Pow)
        assert isinstance(tree.body.left, ast.Name)
        assert isinstance(tree.body.right, ast.Name)
        assert tree.body.right.id == "α"

    def test_caret_braces_become_parentheses(self) -> None:
        """``f^{2}`` 折成 ``f**(2)``。

        ``f**{2}`` 虽然 ``ast`` 能过, 但右边是**集合字面量**, 求值时
        TypeError —— 一个要到运行期才炸的坑。
        """
        tree = ast.parse(rhs_of("X = f^{2}"), mode="eval")
        assert isinstance(tree.body.right, ast.Constant)
        assert tree.body.right.value == 2

    def test_caret_fold_runs_after_exp_fold(self) -> None:
        """``e^x``/``e^(x)`` 仍翻成 ``exp(x)``。

        这是 ``^`` 折叠的**顺序约束**: 折叠必须排在 ``_expand_exp`` 之后。
        若先折, ``e^x`` 变成 ``e**x``, 自然常数底就又会被当成变量。
        """
        for raw in ("E = e^x", "E = e^(x+1)"):
            tree = ast.parse(rhs_of(raw), mode="eval")
            assert isinstance(tree.body, ast.Call), f"{raw!r} -> {ast.dump(tree)}"
            assert isinstance(tree.body.func, ast.Name)
            assert tree.body.func.id == "exp"

    def test_state_qualifier_is_fused_into_symbol(self) -> None:
        """``R_ds(on)`` 必须折成单个符号 ``R_ds_on``, 不留假变量 ``on``。

        ``on``/``off`` 是 MOSFET 状态标注, 不是物理量。不折的话它会被当成
        待查量纲的变量, 公式因「变量 on 未定义」而闭合失败 —— 报错指向
        覆盖度, 掩盖真实原因(限定词没被识别)。
        """
        n = normalize_equation("P_cond = I_rms²·R_ds(on)·D")
        assert n.ok, n.reason
        assert "on" not in n.variables
        assert "R_ds_on" in n.variables
        n2 = normalize_equation("P_cond = I_rms²·R_ds(off)·D")
        assert n2.ok, n2.reason
        assert "off" not in n2.variables
        assert "R_ds_off" in n2.variables

    def test_state_qualifier_fold_spares_real_arguments(self) -> None:
        """实参不是 ``on``/``off`` 时不得改动。

        只认括号内容**恰好**是 ``on``/``off``, 所以 ``exp``/``sqrt``/
        ``min`` 的实参不会被误并进函数名。
        """
        for raw, expect in (
            ("R = exp(-t/τ)", "exp"),
            ("X = min(a, b)", "min"),
            ("Y = sqrt(L/C)", "sqrt"),
        ):
            tree = ast.parse(rhs_of(raw), mode="eval")
            assert isinstance(tree.body, ast.Call), f"{raw!r} -> {ast.dump(tree)}"
            assert isinstance(tree.body.func, ast.Name)
            assert tree.body.func.id == expect

    def test_subscript_comma_inside_parens_is_folded(self) -> None:
        """括号内的下标逗号也必须折 —— 否则符号会被**劈成两个变量**。

        ``P_out/(P_out+P_loss,max)``: 括号内的逗号若不折, Python 把
        ``(P_out+P_loss, max)`` 解析成**元组**, 变量变成 ``P_loss`` 与 ``max``。
        两个名字都合法, 字典里可能真查得到, 于是公式「闭合成功」而实际算的是
        另一个式子 —— 与 ``ω_nsqrt(LC)``、``^`` 被当异或同类。
        """
        n = normalize_equation("P_out/(P_out+P_loss,max)")
        assert n.ok, n.reason
        assert n.variables == ("P_out", "P_loss_max")

    def test_argument_commas_are_not_folded(self) -> None:
        """实参分隔符**不得**被折 —— 判据是逗号前标识符是否已含下划线。"""
        for src, expect in (
            ("X = min(a, b)", {"X", "a", "b"}),
            ("X = max(p, q)", {"X", "p", "q"}),
        ):
            n = normalize_equation(src)
            assert n.ok, n.reason
            assert set(n.variables) == expect
        # 无下划线的括号内逗号不折: (a,b) 是元组, 不是下标限定词
        assert normalize_equation("t = (a,b)").ok

    def test_leading_unary_plus_is_stripped(self) -> None:
        """前导 ``+`` 是「同相/非反相」的书写约定, 不是运算符。

        实测 ``F_J.2.5_ZETA_SEPIC`` 写成 ``+V_in·D/(1-D)`` 与 Boost 的无符号
        写法并列。不去掉会让引擎拒收「不允许的一元运算符 UAdd」。
        """
        n = normalize_equation("+V_in*D/(1-D)")
        assert n.ok, n.reason
        assert n.variables == ("V_in", "D")

    def test_binary_plus_is_untouched(self) -> None:
        """中间的 ``+`` 是二元加号, 碰不得。"""
        n = normalize_equation("X = a + b")
        assert n.ok, n.reason
        assert set(n.variables) == {"X", "a", "b"}
        # 空白在 _translate 里被去掉(``re.sub(r"\\s+", "")``), 故断言无空格形式。
        assert rhs_of("X = a + b") == "a+b"
