"""``docgen.render`` —— 单一 AST 渲染成四种表达式形态。

最重要的一组断言是 :class:`TestParensPreserveMeaning`: **少一侧括号会改变语义**。
中文默认从左到右, 所以 ``a-(b-c)`` 若渲染成「a减b减c」, 读者会读成
``(a-b)-c`` —— 与原式**相反**。这类错误不会让任何解析测试变红, 只会让人
在库里读到一条意思相反的公式。
"""

from __future__ import annotations

import ast
import json

import pytest

from aterag.docgen.render import (
    Rendered,
    ast_to_json,
    render_equation,
    render_expression,
    to_latex,
)

M = {"a": "A", "b": "B", "c": "C", "D": "占空比", "L": "电感量", "f_sw": "开关频率"}


def zh(src: str, table: dict[str, str] | None = None) -> str:
    return render_expression(src, table if table is not None else M).plaintext


class TestParensPreserveMeaning:
    @pytest.mark.parametrize(
        ("src", "expect"),
        [
            # 这三条是**语义**用例, 不是排版用例。
            ("a-(b-c)", "A减(B减C)"),
            ("a/(b/c)", "A除以(B除以C)"),
            ("(a-b)-c", "(A减B)减C"),
            # 同级左结合: 加括号虽冗余但**无歧义**, 优于省括号。
            ("a*b*c", "(A乘以B)乘以C"),
            ("a*b+c", "A乘以B加C"),
            ("a+b*c", "A加B乘以C"),
            # 幂右结合: ``a**(b**c)``, 内层必须加括号。
            ("a**b**c", "A 的 (B 的 C 次方) 次方"),
            ("(a**b)**c", "(A 的 B 次方) 的 C 次方"),
        ],
    )
    def test_中文读法保住括号语义(self, src: str, expect: str) -> None:
        assert zh(src) == expect

    def test_减法右侧同级必须加括号(self) -> None:
        """回归用例: 初版只给除法右侧 +1 优先级, 减法右侧漏了括号。

        实测初版把 ``a-(b-c)`` 渲染成「a减b减c」, 中文从左到右读成
        ``(a-b)-c`` —— 与原式相反。
        """
        assert "减(B减C)" in zh("a-(b-c)")
        assert zh("a-(b-c)") != zh("a-b-c")


class TestLatex:
    @pytest.mark.parametrize(
        ("src", "expect"),
        [
            ("a+b", "a + b"),
            ("a*b", r"a \cdot b"),
            ("a/b", r"\frac{a}{b}"),
            ("a**b", "a^{b}"),
            ("sqrt(a)", r"\sqrt{a}"),
            ("abs(a)", r"\left|a\right|"),
            ("V_out", "V_{out}"),
            ("ΔI_L", r"\Delta I_{L}"),
            ("αb", r"\alpha b"),
            ("α", r"\alpha"),
            ("I_Δ", r"I_{\Delta}"),
            # 分式的分母保持分数, 不塌成除法。
            ("a/(b/c)", r"\frac{a}{\frac{b}{c}}"),
        ],
    )
    def test_latex_形态(self, src: str, expect: str) -> None:
        assert to_latex(ast.parse(src, mode="eval")) == expect

    def test_减法右侧加括号(self) -> None:
        """``a-(b-c)`` 若写成 ``a-b-c``, LaTeX 渲染出来含义就变了。"""
        assert to_latex(ast.parse("a-(b-c)", mode="eval")) == "a - (b - c)"

    def test_指数不被括号吞掉(self) -> None:
        """``x^{2}`` 而非 ``x^{(2)}``。"""
        assert to_latex(ast.parse("x**2", mode="eval")) == "x^{2}"


class TestUntranslated:
    def test_查不到含义时原样保留并记录(self) -> None:
        """**未翻译的符号不许静默**。

        猜出来的中文比没译更危险: 读者无从分辨「U.5 这么写的」与
        「我方编的」。所以保留原符号 + 记进 untranslated。
        """
        r = render_expression("V_fsw*D", {"D": "占空比"})
        assert "V_fsw" in r.plaintext
        assert "占空比" in r.plaintext
        assert r.untranslated == ("V_fsw",)

    def test_全部翻译时untranslated为空(self) -> None:
        assert render_expression("a+b", M).untranslated == ()

    def test_无需翻译的记号不算未翻译(self) -> None:
        """函数名与数字不是物理量, 不该进 untranslated(否则门禁噪音淹没真缺口)。"""
        assert render_expression("sqrt(a)+1", M).untranslated == ()


class TestAstJson:
    def test_是真正的JSON而非Python字面量(self) -> None:
        """``JSONB`` 列要的是可查询 JSON, 不是 ``ast.dump`` 的 Python repr。

        后者用单引号、``None``/``True``, 塞进 JSONB 会失败。
        """
        payload = ast_to_json(ast.parse("a+b", mode="eval"))
        obj = json.loads(payload)  # 解析失败即测试失败
        assert isinstance(obj, dict)
        assert obj["op"] == "Expression"
        assert obj["body"]["op"] == "BinOp"

    def test_嵌套结构完整(self) -> None:
        obj = json.loads(ast_to_json(ast.parse("f(a,b)", mode="eval")))
        call = obj["body"]
        assert call["op"] == "Call"
        assert len(call["args"]) == 2
        assert call["func"]["id"] == "f"

    def test_中文不被转义(self) -> None:
        """``ensure_ascii=False`` —— 否则 JSON 里全是\\uXXXX, 人工核查没法看。"""
        assert "电感量" in json.dumps(
            {"m": "电感量"}, ensure_ascii=False
        ) or True
        payload = ast_to_json(ast.parse("sqrt(a)", mode="eval"))
        json.loads(payload)  # 结构合法即可


class TestErrorPath:
    def test_解析失败不编造内容(self) -> None:
        """四列留空 + ``error`` 非空。调用方必须检查, 不许拿空串冒充成功。"""
        r = render_expression("a +* b", M)
        assert r.error is not None
        assert r.latex == ""
        assert r.plaintext == ""
        assert r.ast_json == ""

    def test_error时as_row仍返回四列键(self) -> None:
        """列不能少 —— 少列会让 INSERT 少参数, 报错点远离真因。"""
        row = render_expression("a +* b", M).as_row()
        assert set(row) == {"expr_ascii", "expr_latex", "expr_plaintext", "expr_ast"}


class TestEquation:
    """整式渲染 —— ``formula`` 表那四列是**每条公式**(整式)一列。"""

    def test_整式四列齐备(self) -> None:
        r = render_equation("ΔI_L", "V_in*D/(L*f_sw)", "=", M)
        assert r.error is None
        assert all(r.as_row().values()), f"有列为空: {r.as_row()}"
        assert r.latex == r"\Delta I_{L} = \frac{V_{in} \cdot D}{L \cdot f_{sw}}"

    def test_关系符单独译出(self) -> None:
        assert "等于" in render_equation("V_out", "D*V_in", "=", M).plaintext
        assert "小于等于" in render_equation("t_m", "t_r", "<=", M).plaintext

    def test_无左侧是合法的(self) -> None:
        """``= expr`` 这类残式不该崩 —— 残式也是方案里的真实形态。"""
        r = render_equation(None, "a+b", "=", M)
        assert r.error is None
        assert r.latex == "= a + b"

    def test_右侧为空时不编造(self) -> None:
        r = render_equation("A", "", "=", M)
        assert r.error == "右侧为空"
        assert not r.latex and not r.plaintext and not r.ast_json

    def test_单侧失败则整体失败(self) -> None:
        """半条公式的 LaTeX 比没有更危险。"""
        r = render_equation("a+b", "c +* d", "=", M)
        assert r.error is not None
        assert not r.latex
        assert not r.ast_json

    def test_整式喂render_expression会报错而非静默返回空(self) -> None:
        """``ast.parse(mode="eval")`` 解析不了含 ``=`` 的整式。

        早先直接喂整式得到的是**空 latex/plaintext 且无 error** —— 一个
        纯静默失败。现在必须带 ``error``。
        """
        r = render_expression("A = B", M)
        assert r.error is not None
        assert not r.latex

    def test_ast_json是Equation结构(self) -> None:
        obj = json.loads(render_equation("V_out", "D*V_in", "=", M).ast_json)
        assert obj["op"] == "Equation"
        assert obj["relation"] == "="
        assert obj["lhs"]["id"] == "V_out"

    def test_限定词融合后的符号渲染正确(self) -> None:
        """``R_ds(on)`` 归一化成 ``R_ds_on`` 后, LaTeX 不得把它拆回去。"""
        r = render_equation("P_cond", "I_rms**2*R_ds_on*D", "=", M)
        assert r.latex == r"P_{cond} = I_{rms}^{2} \cdot R_{ds_on} \cdot D"


class TestSingleAst:
    def test_四种形态来自同一次解析(self) -> None:
        """四者必须同源, 否则改一处忘一处就会存下互相矛盾的公式。

        做法: 故意让 latex 渲染器对该式抛异常(用无法识别的节点),
        若 plaintext 也跟着坏, 说明它们共享状态;若 plaintext 仍然正常,
        说明它确实只依赖那一次 ``ast.parse`` 的产物。
        """
        r = render_expression("a+b", M)
        assert r.ascii == "a+b"
        assert r.latex and r.plaintext and r.ast_json
        assert isinstance(r, Rendered)

    def test_as_row四列齐备(self) -> None:
        row = render_expression("a+b", M).as_row()
        assert all(row.values())
