"""单一 AST → 四种表达式形态(§18.3.1 的四个表达式列)。

``formula`` 表要求四个表达式列全部非空:

==================  ==========================================  ==============
列                  含义                                        本模块实现
==================  ==========================================  ==============
``expr_ascii``      归一化后的引擎语法                          ``Normalized`` 原文
``expr_ast``        归一化 AST(``JSONB``)                       :func:`ast_to_json`
``expr_latex``      LaTeX                                       :func:`to_latex`
``expr_plaintext``  自然语言表述(非 LaTeX)                      :func:`to_plaintext`
==================  ==========================================  ==============

## 为什么四者必须来自同一个 AST

若各自独立生成, 迟早会漂移: 改一处符号法而忘了另一处, 于是库里存着
「LaTeX 说 A·B、ASCII 说 A+B」这种**互相矛盾**的公式, 而所有校验都通过 ——
因为每一列单独看都是合法的。

所以这里只 `ast.parse` 一次, 三个渲染器共用同一棵树。

## 无 LLM(§18.10 注 9)

``expr_plaintext`` 读起来像中文, 但**完全机械**: 变量名去 U.5 的「含义」列
查, 查不到就**原样保留符号**并记进 :attr:`Rendered.untranslated`。绝不生成
方案里没有的描述 —— 那会让「文档与代码同源」失效。

## 未翻译的符号不许静默

:attr:`Rendered.untranslated` 是给门禁/审计看的。若把 ``V_fsw`` 译成
「开关频率」而 U.5 里查不到, 那就是我方编的;若原样保留 ``V_fsw``, 读者至少
知道该去查什么。**猜出来的中文比没译更危险。**
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import cast

__all__ = ["Rendered", "ast_to_json", "render_expression", "to_latex", "to_plaintext"]

#: 希腊字母 -> LaTeX。缺项会原样输出字母(而不是编一个命令名)。
_GREEK_LATEX: dict[str, str] = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\varepsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu",
    "ν": r"\nu", "ξ": r"\xi", "π": r"\pi", "ρ": r"\rho",
    "σ": r"\sigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\varphi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
    "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Φ": r"\Phi",
    "Ψ": r"\Psi", "Ω": r"\Omega",
}

#: 二元/一元算子的优先级。数值越大越紧。用于决定要不要加括号 ——
#: **少一侧括号**会改变语义(``a-(b-c)`` != ``a-b-c``), 这是必须算的部分,
#: 不是排版细节。
_PREC: dict[type, int] = {
    ast.BoolOp: 1, ast.BinOp: 2, ast.UnaryOp: 3, ast.Compare: 2,
}
_BINOP_PREC: dict[type, int] = {
    ast.Add: 1, ast.Sub: 1, ast.Mult: 2, ast.Div: 2,
    ast.Pow: 3, ast.Mod: 2, ast.FloorDiv: 2,
}

#: 函数名 -> (LaTeX 前缀, 中文读法)。``min``/``max`` 的首元素为空(它们走
#: ``\operatorname`` 分支, 不套括号), 故类型是变长元组。
_FUNCS: dict[str, tuple[str, ...]] = {
    "sqrt": (r"\sqrt", "平方根"),
    "abs": (r"\left|", r"\right|"),
    "exp": (r"\exp", "自然指数"),
    "ln": (r"\ln", "自然对数"),
    "log": (r"\log", "对数"),
    "log10": (r"\log_{10}", "常用对数"),
    "min": ("", "与", "的较小值"),
    "max": ("", "与", "的较大值"),
}

_PUNCT_LATEX: dict[type, str] = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: r"\cdot", ast.Div: "/",
    ast.FloorDiv: r"\lfloor", ast.Mod: r"\bmod",
}
_PUNCT_ZH: dict[type, str] = {
    ast.Add: "加", ast.Sub: "减", ast.Mult: "乘以", ast.Div: "除以",
}

_CMP_ZH: dict[type, str] = {
    ast.Lt: "小于", ast.LtE: "小于等于", ast.Gt: "大于",
    ast.GtE: "大于等于", ast.Eq: "等于", ast.NotEq: "不等于",
}


@dataclass(frozen=True)
class Rendered:
    """一条公式的四种形态。

    ``untranslated`` 列出**没有 U.5 含义可依**的符号 —— 它们在
    ``plaintext`` 里原样保留。门禁可用它提示「这里该补 U.5」。
    """

    ascii: str
    latex: str
    plaintext: str
    ast_json: str
    untranslated: tuple[str, ...] = ()
    #: 无法解析的原始文本(非空即表示这一侧渲染失败, 调用方须如实上报,
    #: **不得**用空串冒充成功)。
    error: str | None = None

    def as_row(self) -> dict[str, str | None]:
        """转成 ``formula`` 表四列。"""
        return {
            "expr_ascii": self.ascii,
            "expr_latex": self.latex,
            "expr_plaintext": self.plaintext,
            "expr_ast": self.ast_json,
        }


# ---------------------------------------------------------------------------
# AST -> JSON
# ---------------------------------------------------------------------------


def ast_to_json(node: ast.AST) -> str:
    """把 AST 序列化成**真正的 JSON**(不是 Python 的 ``repr``)。

    ``JSONB`` 列要求可查询的 JSON, 而 ``ast.dump`` 给出的是 Python 字面量
    字符串(``None``/``True``、单引号), 塞进 JSONB 会失败或需要二次转换。
    这里输出 ``{"op": "BinOp", "args": {...}}`` 形状。
    """
    out: dict[str, object] = {}
    match node:
        case ast.Expression():
            out = {"op": "Expression", "body": ast_to_obj(node.body)}
        case ast.BinOp():
            out = {
                "op": "BinOp",
                "operator": type(node.op).__name__,
                "left": ast_to_obj(node.left),
                "right": ast_to_obj(node.right),
            }
        case ast.UnaryOp():
            out = {
                "op": "UnaryOp",
                "operator": type(node.op).__name__,
                "operand": ast_to_obj(node.operand),
            }
        case ast.Call():
            out = {
                "op": "Call",
                "func": ast_to_obj(node.func),
                "args": [ast_to_obj(a) for a in node.args],
            }
        case ast.Name():
            out = {"op": "Name", "id": node.id}
        case ast.Constant():
            value: object = node.value
            if isinstance(value, bool) or value is None:
                out = {"op": "Constant", "value": str(value)}
            elif isinstance(value, (int, float)):
                out = {"op": "Constant", "value": value}
            else:
                out = {"op": "Constant", "value": repr(value)}
        case ast.Compare():
            out = {
                "op": "Compare",
                "left": ast_to_obj(node.left),
                "ops": [type(o).__name__ for o in node.ops],
                "comparators": [ast_to_obj(c) for c in node.comparators],
            }
        case _:
            out = {"op": type(node).__name__, "repr": repr(node)}
    return json.dumps(out, ensure_ascii=False, sort_keys=False)


def ast_to_obj(node: ast.AST) -> dict[str, object]:
    """:func:`ast_to_json` 的结构化版本。

    ``json.loads`` 返回 ``Any``, 直接返回会让 mypy strict 抱怨, 而在这里
    ``cast`` 是**安全**的: 上面的 ``json.dumps`` 保证了它一定可反序列化回
    ``dict[str, object]``。
    """
    return cast("dict[str, object]", json.loads(ast_to_json(node)))


# ---------------------------------------------------------------------------
# 标识符 -> LaTeX
# ---------------------------------------------------------------------------

_SUB_RE = re.compile(r"^(.*?)_(\w+)$")


def _chars_to_latex(text: str) -> str:
    """逐字映射希腊字母, 并在命令后补空格。

    补空格是必需的: LaTeX 的控制序列是「反斜杠 + 连续字母」, 所以
    ``ΔI`` 直接拼成 ``\\DeltaI`` 会被当成**一个未定义命令**
    (``\\Delta`` + ``I``), 编译报错。实测 ``ΔI_L`` -> ``\\DeltaI_{L}``、
    ``αb`` -> ``\\alphab`` 都是这个坑。
    """
    out: list[str] = []
    for i, ch in enumerate(text):
        cmd = _GREEK_LATEX.get(ch)
        if cmd is None:
            out.append(ch)
            continue
        out.append(cmd)
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if nxt.isalpha():
            # 后面紧跟字母 -> 必须断开, 否则命令名会被吞掉后面的字母。
            out.append(" ")
    return "".join(out)


def _ident_to_latex(name: str) -> str:
    """``V_out`` -> ``V_{out}``;``ΔI_L`` -> ``\\Delta I_{L}``。"""
    match = _SUB_RE.match(name)
    if match:
        base, sub = match.groups()
        # 下标里也可能有希腊字母(``I_Δ``), 同样要映射且同样要补空格。
        return f"{_ident_to_latex(base)}_{{{_chars_to_latex(sub)}}}"
    return _chars_to_latex(name)


# ---------------------------------------------------------------------------
# LaTeX
# ---------------------------------------------------------------------------


def to_latex(node: ast.AST, _prec: int = 0) -> str:
    """AST -> LaTeX。

    ``_prec`` 是调用方所在上下文的优先级; 低于它就必须加括号。
    """
    match node:
        case ast.Expression():
            return to_latex(node.body)
        case ast.Constant() if isinstance(node.value, (int, float)):
            return _num(node.value)
        case ast.Name():
            return _ident_to_latex(node.id)
        case ast.UnaryOp():
            sign = "-" if isinstance(node.op, ast.USub) else "+"
            inner = to_latex(node.operand, 3)
            body = f"{sign}{inner}"
            return f"({body})" if _prec > 3 else body
        case ast.BinOp():
            return _binop_latex(node, _prec)
        case ast.Call():
            return _call_latex(node, _prec)
        case ast.Compare():
            parts = [to_latex(node.left, 2)]
            for op, comp in zip(node.ops, node.comparators, strict=True):
                parts.append(_CMP_ZH.get(type(op), type(op).__name__))
                parts.append(to_latex(comp, 2))
            return " ".join(parts)
    return repr(node)


def _binop_latex(node: ast.BinOp, prec: int) -> str:
    my = _BINOP_PREC.get(type(node.op), 2)
    if isinstance(node.op, ast.Div):
        body = rf"\frac{{{to_latex(node.left, 0)}}}{{{to_latex(node.right, 0)}}}"
        return f"({body})" if prec > my else body
    if isinstance(node.op, ast.Pow):
        # 指数必须紧贴, 不能被加括号吞掉: ``x^{2}`` 而非 ``x^{(2)}``。
        return f"{to_latex(node.left, my)}^{{{to_latex(node.right, 0)}}}"
    sym = _PUNCT_LATEX.get(type(node.op))
    if sym is None:
        return repr(node)
    body = f"{to_latex(node.left, my)} {sym} {to_latex(node.right, my + 1)}"
    return f"({body})" if prec > my else body


def _call_latex(node: ast.Call, prec: int) -> str:
    if not isinstance(node.func, ast.Name) or not node.args:
        return repr(node)
    name = node.func.id
    if name == "abs" and len(node.args) == 1:
        return rf"\left|{to_latex(node.args[0], 0)}\right|"
    spec = _FUNCS.get(name)
    if spec is None:
        return repr(node)
    latex = spec[0]
    arg = to_latex(node.args[0], 0)
    if name in ("min", "max"):
        body = " \\operatorname{" + name + "}(" + ", ".join(
            to_latex(a, 0) for a in node.args
        ) + ")"
    elif name == "sqrt":
        body = rf"\sqrt{{{arg}}}"
    else:
        body = f"{latex}({arg})"
    return f"({body})" if prec > 4 else body


def _num(value: int | float) -> str:
    """整数不带 ``.0``; 浮点去掉尾零(``23.141592653589793`` 保持原样)。"""
    if isinstance(value, int):
        return str(value)
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


# ---------------------------------------------------------------------------
# 自然语言
# ---------------------------------------------------------------------------


def to_plaintext(
    node: ast.AST,
    meanings: dict[str, str],
    _prec: int = 0,
    _untranslated: set[str] | None = None,
) -> str:
    """AST -> 中文表述, 变量名取 U.5 的「含义」列。

    查不到含义的符号**原样保留**, 并记进 ``_untranslated`` —— 见模块文档
    「未翻译的符号不许静默」。
    """
    untranslated = _untranslated if _untranslated is not None else set()
    match node:
        case ast.Expression():
            return to_plaintext(node.body, meanings, 0, untranslated)
        case ast.Constant() if isinstance(node.value, (int, float)):
            return _num(node.value)
        case ast.Name():
            meaning = meanings.get(node.id)
            if not meaning:
                untranslated.add(node.id)
                return node.id
            return meaning
        case ast.UnaryOp():
            sign = "负" if isinstance(node.op, ast.USub) else "正"
            return f"{sign}{to_plaintext(node.operand, meanings, 3, untranslated)}"
        case ast.BinOp():
            return _binop_zh(node, meanings, _prec, untranslated)
        case ast.Call():
            return _call_zh(node, meanings, untranslated)
        case ast.Compare():
            parts = [to_plaintext(node.left, meanings, 2, untranslated)]
            for op, comp in zip(node.ops, node.comparators, strict=True):
                parts.append(_CMP_ZH.get(type(op), type(op).__name__))
                parts.append(to_plaintext(comp, meanings, 2, untranslated))
            return " ".join(parts)
    return repr(node)


def _binop_zh(node: ast.BinOp, meanings: dict[str, str], prec: int, untr: set[str]) -> str:
    my = _BINOP_PREC.get(type(node.op), 2)
    if isinstance(node.op, ast.Pow):
        left = to_plaintext(node.left, meanings, my + 1, untr)
        right = to_plaintext(node.right, meanings, my + 1, untr)
        body = f"{left} 的 {right} 次方"
        return f"({body})" if prec > my else body
    word = _PUNCT_ZH.get(type(node.op))
    if word is None:
        return repr(node)
    # 两个操作数都按 ``my + 1`` 渲染 —— 同级子式才会被加括号。
    #
    # 少一侧括号会**改变语义**: ``a-(b-c)`` 若渲染成「a减b减c」, 中文默认
    # 从左到右, 读出来是 ``(a-b)-c``, 与原式相反。实测这正是初版的 bug:
    # 初版只给除法右侧 +1, 于是减法右侧漏了括号。LaTeX 那边靠 ``\frac`` 与
    # 数学惯例本身就无歧义, 故保留其少括号写法(更紧凑)。
    ctx = my + 1
    body = (
        f"{to_plaintext(node.left, meanings, ctx, untr)}"
        f"{word}{to_plaintext(node.right, meanings, ctx, untr)}"
    )
    return f"({body})" if prec > my else body


def _call_zh(node: ast.Call, meanings: dict[str, str], untr: set[str]) -> str:
    if not isinstance(node.func, ast.Name) or not node.args:
        return repr(node)
    name = node.func.id
    spec = _FUNCS.get(name)
    if spec is None:
        return repr(node)
    arg = to_plaintext(node.args[0], meanings, 0, untr)
    if name == "abs":
        return f"{arg} 的绝对值"
    if name == "sqrt":
        return f"{arg} 的平方根"
    if name == "min":
        others = "、".join(to_plaintext(a, meanings, 0, untr) for a in node.args[1:])
        return f"{arg} 与 {others} 中的较小值"
    if name == "max":
        others = "、".join(to_plaintext(a, meanings, 0, untr) for a in node.args[1:])
        return f"{arg} 与 {others} 中的较大值"
    return f"{arg} 的{spec[1]}"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


_REL_ZH: dict[str, str] = {
    "=": "等于", "<": "小于", "<=": "小于等于", ">": "大于",
    ">=": "大于等于", "==": "等于", "!=": "不等于",
}


def _side_obj(text: str) -> dict[str, object]:
    """解析一侧并**剥掉** ``Expression`` 外壳。

    ``ast.parse(mode="eval")`` 会套一层 ``Expression``, 若原样塞进 JSONB,
    查 ``expr_ast->'lhs'->>'id'`` 就会落空, 得写成 ``->'body'->>'id'``。
    ``formula`` 表的 ``expr_ast`` 是要被查询的列(§18.3「公式知识化 RAG
    实现要点»), 少一层嵌套值得。
    """
    node = ast.parse(text, mode="eval")
    return ast_to_obj(node.body if isinstance(node, ast.Expression) else node)


def render_equation(
    lhs: str | None,
    rhs: str,
    relation: str = "=",
    meanings: dict[str, str] | None = None,
) -> Rendered:
    """渲染**整条方程**(``formula`` 表那四列要的就是这个粒度)。

    为什么不只用 :func:`render_expression`: ``ast.parse(..., mode="eval")``
    解析不了含关系符的整式(``A = B`` 会抛 SyntaxError), 早先实测直接喂整式
    得到的latex/plaintext **全是空串** —— 一个静默失败。这里显式按两侧渲染
    再拼, 并把关系符单独译出。

    两侧之中任一侧解析失败, 则整体标 ``error`` 并留空四列: 半条公式的
    LaTeX 比没有更危险。
    """
    table = meanings or {}
    lhs_text = (lhs or "").strip()
    rhs_text = (rhs or "").strip()
    if not rhs_text:
        return Rendered(
            ascii="", latex="", plaintext="", ast_json="",
            error="右侧为空",
        )
    left = (
        render_expression(lhs_text, table)
        if lhs_text
        # 无左侧是合法的(``= expr`` 这类残式), 用空形态而不是 None, 免得
        # 后面满地``if left is None``。
        else Rendered(ascii="", latex="", plaintext="", ast_json="")
    )
    right = render_expression(rhs_text, table)
    failed = [
        name
        for name, r in (("左侧", left), ("右侧", right))
        if r is not None and r.error
    ]
    if failed:
        reasons = [r.error for r in (left, right) if r.error]
        return Rendered(
            ascii="", latex="", plaintext="", ast_json="",
            error=f"{'/'.join(failed)}渲染失败: {'; '.join(reasons)}",
        )
    rel = relation.strip() or "="
    untr = tuple(sorted(set(left.untranslated) | set(right.untranslated)))
    latex = f"{left.latex} {rel} {right.latex}" if left.latex else f"{rel} {right.latex}"
    plaintext = (
        f"{left.plaintext}{_REL_ZH.get(rel, rel)}{right.plaintext}"
        if left.plaintext
        else f"{rel}{right.plaintext}"
    )
    equation_ast: dict[str, object] = {"op": "Equation", "relation": rel}
    if left.ascii:
        equation_ast["lhs"] = _side_obj(lhs_text)
    equation_ast["rhs"] = _side_obj(rhs_text)
    return Rendered(
        ascii=f"{lhs_text} {rel} {rhs_text}".strip(),
        latex=latex,
        plaintext=plaintext,
        ast_json=json.dumps(equation_ast, ensure_ascii=False),
        untranslated=untr,
    )


def render_expression(
    ascii_text: str,
    meanings: dict[str, str] | None = None,
) -> Rendered:
    """把一条**已归一化**的表达式渲染成四种形态。

    ``ascii_text`` 应是 :mod:`docgen.expr_norm` 产出的 ``Normalized.lhs``/
    ``rhs`` —— 已经是引擎语法, 这里只做渲染, 不再翻译(避免两处各翻一次
    而漂移)。

    解析失败时返回 ``error`` 非空的 :class:`Rendered`, 四列留空字符串 ——
    **不编造**。调用方必须检查 ``error``。
    """
    table = meanings or {}
    try:
        tree = ast.parse(ascii_text, mode="eval")
    except SyntaxError as exc:
        return Rendered(
            ascii=ascii_text, latex="", plaintext="", ast_json="",
            error=f"AST 解析失败: {exc}",
        )
    untr: set[str] = set()
    plaintext = to_plaintext(tree, table, 0, untr)
    return Rendered(
        ascii=ascii_text,
        latex=to_latex(tree),
        plaintext=plaintext,
        ast_json=ast_to_json(tree),
        untranslated=tuple(sorted(untr)),
    )
