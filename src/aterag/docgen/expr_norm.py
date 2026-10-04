"""语料表达式 -> 量纲引擎语法 (§18.3.4 管线第 3 步 normalize 的前置)。

为什么必须有这一层
------------------
引擎的 ``_parse`` 用 ``ast.parse(mode="eval")``, 只认 Python 表达式语法
(数值、变量名、``+ - * / **``、一元负号、白名单 ``sqrt/abs/min/max``)。而方案
里写的是 ``V_out = D · V_in``、``ΔI_L = (V_on·D)/(L·f_sw)``、``√(2πLC)``。

实测两条语法**互斥**: 457 条去重后的等式中 ``ast.parse`` 直接接受 **1 条**;
即便只取 ``=`` 右侧, 也只接受 157 条。不做归一化就把语料喂给引擎, 得到的会是
几乎 100% 的 ``ExpressionError``, 而报错指向引擎, 不指向「语料与引擎语法不同」
这个真实原因。

一个单元格里常常是**多条公式**
-----------------------------
实测: 表达式列里大量单元格用反引号分隔着好几条公式, 例如::

    `ω_z,esr = 1/(ESR·C_out)`，`f_esr = ω_z,esr/(2π)`
    `V_out = D·V_in`（半桥，`D_eff = 2D`）

所以清洗后先按反引号分段, 取**第一段不含中文且带顶层关系符的**作为该 ID 的
表达式 —— 语料里主公式一律写在最前 (``F_J.6.1`` 是 ``K = 2·L·f_sw/R_load``
后面才跟 ``K>1 CCM``; ``F_M.1.4`` 是 ``U = k·u_c`` 后面才跟 ``k=2``)。
其余段落在 :attr:`Normalized.extra_segments` 里计数上报, 不静默丢弃。

翻译的边界: 只做可逆的记号替换, 不改数学结构
--------------------------------------------
替换表逐条显式枚举。刻意**不**做的事:

* 不把 ``≲``/``≈`` 之类关系改写成等号 (那会伪造方程)
* 不给 ``√LC`` 里的 ``LC`` 补乘号 (分不清「电感×电容」与一个叫 ``LC`` 的量)
* 不认 ``e`` 作自然常数底 —— ``e^(...)`` 翻成 ``exp(...)``, **由引擎**判定
  能否接受, 本模块不重复维护函数白名单
* 不把 ``K*``/``D*`` 的星号丢掉 (``K*`` 是修正值, 丢成 ``K`` 就是伪造恒等)

踩过的坑, 每条都是「返回了看起来合法的结果但数学是错的」
---------------------------------------------------------
* ``2π`` 若先折成数值常量, 会得到 ``23.141592653589793`` —— 多项式被粘成一个
  巨大常数, ``ast`` 照样通过。故先换成标识符占位、补完隐含乘法, 最后才代入。
* ``)`` 后无条件补 ``*`` 会把 ``(a)/(b)`` 变成 ``(a)*/(b)``。补乘号必须同时看
  右边是什么。
* 补乘号只在「数字/``)`` 紧跟标识符或 ``(``」时插。``√`` 展开后残留的空格也会
  让两个记号相邻。
* 下标数字 (``I₁`` 的 ₁ 是 U+2081) 与上标 (``A²`` 的 ² 是 U+00B2) 都不是
  ``[0-9]``, 不折算则整段丢失。下标折成 ``_1`` 形式 (``I₁`` -> ``I_1``),
  **保留**区分度: 折成 ``I`` 会把 ``I₁`` 和 ``I₂`` 合并成一个量。

无法归一化时返回 ``ok=False`` 并给出**具体**原因, 绝不返回半成品。原因是
给门禁与人看的, 必须能定位到具体原因, 而不是一句「无法解析」。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

__all__ = ["Normalized", "normalize_equation", "RELATION_TOKENS"]

#: 圆周率。无量纲。先落成标识符占位, 等隐含乘法补完再代入数值常量 ——
#: 直接替换会把 ``2π`` 粘成 ``23.14…``, 数学全错而 ``ast`` 通过。
#:
#: 占位符**不能含下划线**: ``_clean`` 里有一条把 ``__`` 折叠成 ``_`` 的规则
#: (下标折叠会产生 ``I__1``), 它会把占位符一起改掉, 于是最后的代入落空 ——
#: ``π`` 就以 ``_pi_const_`` 这个变量名留在表达式里, 顺带把后面的 ``sqrt``
#: 粘成一个标识符。
PI_SENTINEL = "PIconst"
PI_LITERAL = "3.141592653589793"

#: 关系符, **长的在前**。``⟹`` 必须先于 ``⇒``/``→``; ``≈`` 必须先于 ``=``。
RELATION_TOKENS: tuple[str, ...] = (
    "⟹",
    "⟺",
    "⟷",  # U+27F7 长双向箭头
    "⇒",
    "→",
    "≪",
    "≲",
    "≤",
    "≥",
    "≈",
    "=",
    "≠",
    # ASCII 的 ``<`` / ``>`` 必须在最后: 语料里链式不等式
    # (``AL_LL < AL_L < x_min < x_max``) 很常见, 不认就会把整串当成一个表达式,
    # 然后 ``ast`` 报错 —— 而真实原因是「这是链式关系式, 引擎校验不了」。
    "<",
    ">",
)

#: Latin-1 补充区的上标数字。**码点不连续**, 不能写成区间:
#: ``²``=U+00B2、``³``=U+00B3、``¹``=U+00B9 —— 而 U+00B0 是**度符号**,
#: 不是上标零。写成 ``0x00B0..0x00B3`` 会漏掉 ``¹``(U+00B9 在区间外) 并把
#: 度符号误认成上标零, 实测 ``T⁻¹`` 因此整条解析失败。
_LATIN1_SUPERSCRIPT = {0x00B2: "2", 0x00B3: "3", 0x00B9: "1"}


def _fold_superscripts(s: str) -> str:
    """上标 -> **幂**。

    关键在于**要不要补 ``**``**。早先一版把 ``²`` 直接翻成 ``2``, 于是
    ``(1-D)²`` 变成 ``(1-D)2`` —— 不是合法表达式, 而如果碰巧两边都是标识符,
    它会变成 ``(1-D)x2`` 这种**语义完全不同的合法表达式**。所以幂号必须补。

    只在上标**紧跟一个值** (标识符字符、数字、``)``、``]``) 时补 ``**``;
    跟在运算符后面的数字不是幂。
    """
    out: list[str] = []
    for i, ch in enumerate(s):
        cp = ord(ch)
        # 上标字符分**两个区**, 漏掉任何一个都会静默失效:
        #   U+2070..U+2079  上标 0-9 与 ⁻⁺ (⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺)
        #   U+00B0..U+00B3  上标 0-3 与 ¹²³ (¹=U+00B9, ²=U+00B2, ³=U+00B3)
        # 语料里两种都出现: ``THD_I²`` 用 U+00B2, ``T⁻¹`` 用 U+207B+U+00B9。
        # 早先一版只覆盖 U+207x 区, 于是 41 条含 ``²`` 的公式全部解析失败。
        digit: str | None = None
        if 0x2070 <= cp <= 0x2079:
            digit = str(cp - 0x2070)
        elif cp in _LATIN1_SUPERSCRIPT:
            digit = _LATIN1_SUPERSCRIPT[cp]
        elif cp == 0x207B:
            digit = "-"
        elif cp == 0x207A:
            digit = "+"
        prev = s[i - 1] if i else ""
        if digit is not None:
            # 上标紧跟一个**值**时它是幂, 必须补 ``**``。
            # 注意不能靠 ``ch.isdigit()`` 判断后续分支: ``₁``.isdigit() 在
            # Python 里是 **True**, 于是下标会被当幂读成 ``I**1``。
            if prev and (prev.isalnum() or prev in ")]_"):
                out.append(f"**{digit}")
            else:
                out.append(digit)
            continue
        # ASCII 数字**永不**补 ``**``: 它跟在数字后面时是同一个数 (``10`` 不是
        # ``1**0``)。早先一版在这里也补, 于是 ``100%`` 变成 ``1**0**0`` ——
        # 每个多位整数都被拆成幂运算, 而 ``ast`` 照样通过。
        out.append(ch)
    return "".join(out)


def _fold_subscripts(s: str) -> str:
    """下标数字 (U+2080..U+2089) -> ``_N``。

    保留区分度: ``I₁`` -> ``I_1`` 而不是 ``I`` —— 折成 ``I`` 会把 ``I₁`` 与
    ``I₂`` 合并成同一个量, 而两者的量纲未必相同 (例如峰值与有效值)。

    用码点循环而不是在源码里直接写这些字符: 下标字符极易在编辑或传输中被改写
    成别的码位, 一旦被改写, 映射就静默错位。

    用 ``replace`` 而不是 ``str.maketrans``: 这里是 1 -> 2 (``₁`` -> ``_1``),
    而 ``maketrans`` 只支持 1 -> 1。
    """
    for k in range(10):
        s = s.replace(chr(0x2080 + k), f"_{k}")
    return s


#: 排版记号 -> ASCII 运算符。
_OPERATOR_MAP = (
    ("·", "*"),  # U+00B7 中点
    ("⋅", "*"),  # U+22C5 点乘
    ("×", "*"),  # U+00D7
    ("∗", "*"),  # U+2217
    ("−", "-"),  # U+2212 减号 (**不是** ASCII 的 -)
    ("–", "-"),  # U+2013
    ("—", "-"),  # U+2014
    ("÷", "/"),
)

#: 引擎无对应节点的算子。积分的量纲其实可算 (``∫f dx -> dim f · dim x``),
#: 但引擎没有积分节点, 故如实报「不支持」而不是假装能做。
#:
#: **``→`` 不在此列**: 它是关系符 (蕴含/趋于), 已在 :data:`RELATION_TOKENS` 里。
#: 早先一版把它同时列进这里与关系符表, 于是 ``s →(1−z⁻¹)/T_s`` 这类在切分
#: **之前**就被拒, 白丢 7 条 —— 报的还是「含极限记号」这种驴唇不对马嘴的原因。
_UNSUPPORTED_OPS: tuple[tuple[str, str], ...] = (
    ("∫", "含积分"),
    ("∬", "含积分"),
    ("∮", "含积分"),
    ("∑", "含求和"),
    ("∏", "含连乘"),
    ("∞", "含无穷"),
    ("∂", "含偏导"),
)

#: 出现即说明这**不是方程**的记号。逐个给理由, 不合并成一句「无法解析」。
_NON_EQUATION_OPS: tuple[tuple[str, str], ...] = (
    ("∝", "含正比记号 ∝(比例常数未知, 不是方程)"),
    ("∈", "含属于记号 ∈"),
    ("∀", "含量词记号 ∀"),
    ("∃", "含量词记号 ∃"),
)

#: 星号记号的**修饰**形态: ``D*`` / ``K*`` (修正值) 与 ``z*`` / ``y*`` (复共轭)。
#:
#: 不能简单按「出现 ``*``」判定 —— ``_clean`` 已经把 ``·``/``×`` 全映射成
#: ``*``, 那样会把 243 条正常公式全部误杀 (实测踩过)。也不能折成乘号:
#: ``z*`` 是复共轭, 当乘号就凭空多出一个星号因子; 折成 ``z_star`` 则会把共轭
#: 与「修正后的值」混成同一个符号 (实测污染出 10 个 ``*_star`` 假变量)。
#:
#: 判据: 左边是标识符字符, 右边**不是**任何记号的开头 —— 于是
#: ``D*`` / ``D*/V`` / ``z*`` 命中, 而 ``a*b`` 与 ``sqrt(2)*`` 不命中。
#:
#: **只在原始文本上判定** (``normalize_equation`` 入口处), 不在清洗后的文本上。
#: 清洗会把 ``·``/``×`` 全映射成 ``*``, 于是 ``z*·H`` 变成 ``z**H``, 而幂
#: 运算符 ``**`` 与星号修饰长得一模一样 —— 排除 ``*`` 会连带把真正的共轭
#: ``z*`` 一起放过 (实测 105 条误杀换 7 条漏放)。在原文上判就没有这个歧义:
#: 原文里的 ASCII ``*`` 只可能是星号记号。
_STAR_MODIFIER_RE = re.compile(r"(?<=[A-Za-z0-9_Ͱ-Ͽ\]])\*(?![0-9A-Za-z_Ͱ-Ͽ(.])")

#: 区间记号: ``1.5~2.5`` / ``10~90`` / ``10% ~ I_set``。这是「取值范围」而不是
#: 算式, 且 ``~`` 若折成减号会得到 ``1.5-2.5`` —— 一个合法但语义完全不同的表达式。
#: 右端不限定为数字: 语料里还有 ``≤10% ~ I_set`` 这种「百分比到量」的区间。
_RANGE_RE = re.compile(r"[0-9A-Za-z]\s*~\s*[0-9A-Za-z]")

#: 引擎白名单只有 sqrt/abs/min/max。出现在「名字紧跟左括号」位置时, 说明这是
#: 函数而不是变量 —— 放过去会被当成待查量纲的物理量, 然后以「变量不在符号表」
#: 被排除, 报错指向符号表覆盖度, 掩盖了真正原因 (引擎不支持该函数)。
_TRANSCENDENTAL = (
    "exp",
    "log",
    "log10",
    "log_10",
    "ln",
    "lg",
    "sin",
    "cos",
    "tan",
    "cot",
    "arcsin",
    "arccos",
    "arctan",
    "sinh",
    "cosh",
    "tanh",
)
_TRANSCENDENTAL_RE = re.compile(rf"\b(?:{'|'.join(_TRANSCENDENTAL)})\s*\(")

#: CJK 与全角标点。用来判「这根本不是公式」。
_CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
_IDENT_START = r"[A-Za-z_\u0370-\u03ff]"
#: 完整标识符的模式**源码** (未编译) —— ``_protect_star_modifier`` 要把它嵌进
#: 另一个正则里, 而已编译的 ``re.Pattern`` 不可切片取模式串。
_IDENT_PATTERN = rf"{_IDENT_START}[A-Za-z0-9_\u0370-\u03ff]*"
_IDENT_RE = re.compile(_IDENT_PATTERN)
_IDENT_START_RE = re.compile(_IDENT_START)
_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


@dataclass(frozen=True)
class Normalized:
    """一条等式的归一化结果。

    ``ok=False`` 时 :attr:`reason` 必非空, 且 ``lhs``/``rhs`` 为 ``None`` ——
    宁可交回一个空结果加一句原因, 也不要交回「看着像表达式」的半成品。
    """

    ok: bool
    lhs: str | None = None
    rhs: str | None = None
    relation: str | None = None
    #: 归一化后 AST 里的全部变量名 (已去重, 顺序按出现)
    variables: tuple[str, ...] = ()
    #: 原文里识别出的变量名 —— 与 :attr:`variables` 供人工比对
    source_variables: tuple[str, ...] = ()
    #: 原单元格里被跳过的其余公式段数 (见模块 docstring)
    extra_segments: int = 0
    reason: str | None = None

    @property
    def equation(self) -> str | None:
        """拼回 ``lhs rel rhs``, 供人读。"""
        if self.rhs is None:
            return None
        if self.lhs is None:
            return self.rhs
        return f"{self.lhs} {self.relation} {self.rhs}"


# ---------------------------------------------------------------------------
# 清洗
# ---------------------------------------------------------------------------


def _clean(expr: str) -> str:
    """剥包裹记号与排版记号。**保留**内部反引号 —— 它们是公式段的分隔符。"""
    s = expr.strip().strip("`").strip()
    if s.startswith("$") and s.endswith("$") and len(s) >= 2:
        s = s[1:-1]
    for full, half in (("＝", "="), ("－", "-"), ("＋", "+"), ("＜", "<"), ("＞", ">")):
        s = s.replace(full, half)
    s = s.replace("（", "(").replace("）", ")").replace("［", "[").replace("］", "]")
    s = s.replace("，", ",").replace("；", ";").replace("：", ":")
    # LaTeX 范数记号 -> abs()。``\|X\|`` 是绝对值/模长, 而 ``abs`` 正在引擎
    # 白名单里。**必须在含反斜杠的拒绝之前做**, 否则这批 (实测 16 条) 会被
    # 「含 LaTeX 反斜杠」一刀切掉 —— 而它们其实是完全可校验的。
    # 模式写成 ``\\\|``: 第一个 ``\\`` 匹配一个字面反斜杠, ``\|`` 匹配字面竖线。
    s = re.sub(r"\\\|(.+?)\\\|", r"abs(\1)", s)
    s = re.sub(r"\\lVert(.+?)\\rVert", r"abs(\1)", s)
    s = _fold_superscripts(s)
    s = _fold_subscripts(s)
    # 折叠 ``__`` : ``I_₁`` 先变 ``I__1`` 再变 ``I_1``。
    # 必须排在下面 π 占位符替换**之前**, 否则占位符会被一起改掉。
    s = re.sub(r"_{2,}", "_", s)
    s = s.replace("π", PI_SENTINEL)
    s = s.replace("½", "0.5")
    # ``90°`` 的度数符号: 跟在数值后的角度标注, 丢掉不改变量纲
    s = re.sub(r"(?<=[0-9)\]])°", "", s)
    # ``·100%`` 里的百分号是**无量纲**的比例标注, 去掉不改变量纲;
    # 但 ``%GRR`` 这种「百分号在前」不是数值后缀, 不动它。
    s = re.sub(r"(?<=[0-9)])\s*%", "", s)
    for src, dst in _OPERATOR_MAP:
        s = s.replace(src, dst)
    # ASCII 引号是表格里的排版残留 (``K = 2·L·f_sw/R_load'``), Python 会当成
    # 字符串起始, 于是报「unterminated string literal」—— 一个与真实原因
    # (多了个引号) 毫无关系的报错。
    #
    # LaTeX 里 ``x'`` 表示导数, 但语料写导数一律用 ``∂`` (已被「含偏导」拒),
    # 所以这里删掉 ``'`` 不会吃掉任何导数记号。
    s = s.replace("'", "").replace('"', "")
    return s.strip()


# ---------------------------------------------------------------------------
# 关系符切分
# ---------------------------------------------------------------------------


def _find_relation(s: str, start: int = 0) -> tuple[int, str] | None:
    """找**顶层** (括号深度 0) 的第一个关系符。

    深度必须计入, 否则 ``f(a = 1)`` 这类会被切开。语料里没有这种写法, 计入
    是为了让规则本身站得住, 而不是靠「恰好没有反例」。
    """
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0:
            for tok in RELATION_TOKENS:
                if s.startswith(tok, i):
                    return i, tok
    return None


def _split_relation(s: str) -> tuple[str | None, str | None, str | None]:
    hit = _find_relation(s)
    if hit is None:
        return None, None, s.strip()
    i, tok = hit
    return s[:i].strip(), tok, s[i + len(tok) :].strip()


# ---------------------------------------------------------------------------
# 记号翻译
# ---------------------------------------------------------------------------


def _balanced(s: str, i: int) -> int:
    """``s[i]`` 是左括号时, 返回配对右括号之后的下标; 找不到则返回 len(s)。

    不按类型配对 (``(]`` 也算闭合) —— 语料里没有混用嵌套, 而按类型配对会引入
    一个「不匹配就怎么办」的分支, 而这个分支的正确答案只能是「不闭合」。
    """
    pairs = {"(", "["}
    depth = 0
    for k in range(i, len(s)):
        if s[k] in pairs:
            depth += 1
        elif s[k] in ")]":
            depth -= 1
            if depth == 0:
                return k + 1
    return len(s)


def _operand(s: str, j: int) -> tuple[str, int] | None:
    """取 ``s[j:]`` 开头的一个记号 (括号组 / 标识符 / 数字)。

    括号组返回**去壳**的内容 (``(a+b)`` -> ``a+b``) —— 调用方本来就要把它
    嵌进 ``f(...)`` 里, 不去壳就会得到 ``exp((a/b))`` 这种双层括号。它会
    直接进 ``expr_ascii`` 列, 可读性也是产物的一部分。
    """
    if j >= len(s):
        return None
    if s[j] in "([":
        end = _balanced(s, j)
        inner = s[j + 1 : end - 1] if end <= len(s) and s[end - 1 : end] in ")]" else s[j:end]
        return inner, end
    m = _IDENT_RE.match(s, j) or _NUMBER_RE.match(s, j)
    if not m:
        return None
    return m.group(0), m.end()


def _expand_sqrt(s: str) -> str:
    """``√X`` -> ``sqrt(X)``。操作数取紧跟其后的**一个**记号。

    刻意不做 ``√ab -> sqrt(a*b)``: 语料里 ``√(2πLC)`` 写了括号, 而 ``√ab``
    分不清「a×b」与「一个叫 ab 的量」, 补乘号就是替方案做数学决定。
    """
    if "√" not in s:
        return s
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] != "√":
            out.append(s[i])
            i += 1
            continue
        j = i + 1
        while j < len(s) and s[j].isspace():
            j += 1
        got = _operand(s, j)
        if got is None:
            out.append(s[i])
            i += 1
            continue
        text, end = got
        # 前一个字符是标识符字符时必须补乘号: ``ω_n√(LC)`` 直接拼成
        # ``ω_nsqrt(LC)`` —— 一个**合法但完全不同**的标识符, 比解析失败更糟
        # (它会拿着名字 ``ω_nsqrt`` 去查量纲字典, 然后报「变量未定义」)。
        tail = "".join(out)
        needs_star = bool(tail) and (tail[-1].isalnum() or tail[-1] in "_)" or tail[-1] == "√")
        out.append(("*" if needs_star else "") + f"sqrt({text})")
        i = end
    return "".join(out)


def _expand_exp(s: str) -> str:
    """``e^x`` / ``e^{x}`` / ``e(x)`` -> ``exp(x)``。

    翻成 ``exp`` 而不是保留 ``e``: ``e`` 是合法 Python 标识符, 留着会被当成
    一个待查量纲的变量, 于是公式以「变量 e 不在符号表里」被排除, 报错指向
    符号表覆盖度而非引擎白名单。翻过去之后由**引擎**判定, 白名单只有一处。
    """
    if "e" not in s:
        return s
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "e" and i + 1 < len(s) and s[i + 1] in "^(":
            got = _operand(s, i + 2)
            if got is not None:
                text, end = got
                out.append(f"exp({text})")
                i = end
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _insert_implicit_multiplication(s: str) -> str:
    """在「数字或 ``)`` 紧跟标识符/``(``」之间补 ``*``。

    ``2πfC`` 折成常量后会变成 ``2`` 与 ``f`` 相邻的非法记号, 必须补乘号 ——
    ``√(2πLC)`` 这类尤其常见。

    补乘号的**两个方向都要看**: 只看左边会在 ``(a)/(b)`` 里插出 ``(a)*/(b)``;
    只看右边会在 ``2*π`` 上重复插入。
    """
    out: list[str] = []
    for i, ch in enumerate(s):
        prev = s[i - 1] if i else ""
        if prev and prev not in "*/+-^=<>,()[] ":
            cur_is_value = bool(_IDENT_START_RE.match(ch)) or ch == "("
            if (prev.isdigit() or prev == ")") and cur_is_value:
                out.append("*")
        out.append(ch)
    return "".join(out)


def _fold_subscript_comma(s: str) -> str:
    """下标里的逗号 -> 下划线: ``t_relay,op`` -> ``t_relay_op``。

    方案用 ``t_relay,op`` 表示「继电器动作时间」(下标里的第二个限定词), 但
    Python 标识符不能含逗号。

    **只在括号外做。** 括号里的逗号是实参分隔符 (``min(a, b)``), 一并改掉会
    把两个参数粘成一个 —— 那是个合法但语义完全不同的表达式, 比原样失败糟得多。
    """
    out: list[str] = []
    depth = 0
    for i, ch in enumerate(s):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if (
            ch == ","
            and depth == 0
            and i > 0
            and i + 1 < len(s)
            and _IDENT_START_RE.match(s[i + 1])
            and (s[i - 1].isalnum() or s[i - 1] == "_")
        ):
            out.append("_")
            continue
        out.append(ch)
    return "".join(out)


def _translate(s: str) -> str:
    """把一侧的文本翻成 Python 表达式语法 (不含合法性校验)。"""
    t = _expand_sqrt(s)
    t = _expand_exp(t)
    t = _insert_implicit_multiplication(t)
    t = _fold_subscript_comma(t)
    t = re.sub(r"\s+", "", t)
    # 占位符必须在补乘号**之后**才代入, 否则 ``2π`` 会粘成 ``23.14…``
    return t.replace(PI_SENTINEL, PI_LITERAL).strip()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


class _VarCollector(ast.NodeVisitor):
    """收集表达式里的**变量**名。

    必须特判 ``Call``: ``ast.walk`` 会把 ``sqrt``/``abs``/``min``/``max`` 这些
    **函数名**也当成 ``ast.Name`` 收进来, 于是它们会被当成待查量纲的物理量,
    在符号表里查不到, 公式因「变量 sqrt 未定义」而闭合失败 —— 一个纯粹由
    工具缺陷造出来的失败。早先一版就是这么把 ``sqrt``(28 次) 与 ``abs``
    (12 次) 混进「缺失变量」清单的。
    """

    def __init__(self) -> None:
        self.names: list[str] = []

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        self.names.append(node.id)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        # 只下探实参, 跳过 func —— 那不是变量
        for arg in node.args:
            self.visit(arg)


def _ast_variables(expr: str) -> tuple[str, ...]:
    collector = _VarCollector()
    collector.visit(ast.parse(expr, mode="eval"))
    return tuple(dict.fromkeys(collector.names))


def _source_variables(text: str) -> tuple[str, ...]:
    """从**原文**一侧取变量名 (含希腊字母), 用于与归一化结果比对。"""
    scrubbed = re.sub(r"[0-9.]+", " ", text)
    scrubbed = scrubbed.replace(PI_SENTINEL, " pi ")
    return tuple(dict.fromkeys(m.group(0) for m in _IDENT_RE.finditer(scrubbed)))


def _reject_reason(expr: str) -> str | None:
    """先判「根本不是公式」与「引擎无节点」两类, 早于任何翻译。"""
    if not expr:
        return "空表达式"
    if "\\" in expr:
        return "含 LaTeX 反斜杠"
    if _CJK_RE.search(expr):
        return "含中文叙述, 不是纯公式"
    for ch, label in _UNSUPPORTED_OPS:
        if ch in expr:
            return f"{label}(引擎无对应节点)"
    for ch, label in _NON_EQUATION_OPS:
        if ch in expr:
            return label
    if _RANGE_RE.search(expr):
        return "含区间记号 ~(取值范围, 不是算式)"
    if _TRANSCENDENTAL_RE.search(expr):
        return "含超越函数(引擎白名单仅 sqrt/abs/min/max)"
    return None


def _parse_side(expr: str | None, label: str) -> tuple[tuple[str, ...] | None, str | None]:
    if expr is None:
        return None, None
    try:
        return _ast_variables(expr), None
    except SyntaxError as exc:
        return None, f"ast 解析失败({label}): {exc.msg}"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def normalize_equation(expr: str | None) -> Normalized:
    """把一条语料等式归一化成引擎可解析的形式。纯函数, 不碰文件与数据库。"""
    if not expr:
        return Normalized(ok=False, reason="空表达式")

    # 星号修饰只在**原文**上判, 理由见 _STAR_MODIFIER_RE 的说明。
    if _STAR_MODIFIER_RE.search(expr):
        return Normalized(
            ok=False,
            reason="含星号记号 *(修正值 K*/D* 与共轭 z* 语义不同, 不能一律当乘号)",
            source_variables=_source_variables(_clean(expr)),
        )

    cleaned = _clean(expr)

    # 一个单元格常含多条公式 (反引号分隔)。取第一段「干净且带顶层关系符」的。
    segments = [seg.strip() for seg in cleaned.split("`") if seg.strip()]
    if not segments:
        return Normalized(ok=False, reason="空表达式")
    chosen: str | None = None
    for seg in segments:
        if _reject_reason(seg) is None and _find_relation(seg) is not None:
            chosen = seg
            break
    extra = len(segments) - 1
    if chosen is None:
        # 没有一段是干净方程: 退回第一段, 让报错信息反映真实内容
        chosen = segments[0]
        extra = len(segments) - 1

    reason = _reject_reason(chosen)
    if reason:
        return Normalized(ok=False, reason=reason, extra_segments=extra)

    lhs_raw, rel, rhs_raw = _split_relation(chosen)
    if rel is not None and not rhs_raw:
        return Normalized(
            ok=False, reason=f"关系符 {rel!r} 右侧为空", extra_segments=extra
        )

    lhs = _translate(lhs_raw) if lhs_raw else None
    rhs = _translate(rhs_raw) if rhs_raw else None
    if not rhs:
        return Normalized(ok=False, reason="右侧为空", extra_segments=extra)

    # 链式关系 (A < B < C): 语义是「各项同量纲」, 但引擎只能校验单个表达式。
    # 改写成等号就是伪造方程, 所以如实报不支持。
    for side_name, text in (("左侧", lhs), ("右侧", rhs)):
        if text and _find_relation(text) is not None:
            return Normalized(
                ok=False,
                reason=f"链式关系式({side_name}仍含关系符)",
                extra_segments=extra,
                source_variables=_source_variables(chosen),
            )

    variables: tuple[str, ...] = ()
    for label, text in (("左侧", lhs), ("右侧", rhs)):
        vs, err = _parse_side(text, label)
        if err:
            return Normalized(
                ok=False,
                reason=err,
                extra_segments=extra,
                source_variables=_source_variables(chosen),
            )
        variables = tuple(dict.fromkeys((*variables, *(vs or ()))))

    return Normalized(
        ok=True,
        lhs=lhs,
        rhs=rhs,
        relation=rel,
        variables=variables,
        source_variables=_source_variables(chosen),
        extra_segments=extra,
    )
