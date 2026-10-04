"""U.5 全局符号表 -> 可按命名空间解析的符号字典 (W1 量纲闭合的输入)。

为什么要单独解析一张表
--------------------
§18.3.4 的 ``check_dimension`` 要「代入变量量纲校验齐次性」, 而 549 条候选
公式里只有 **114 条**带量纲列 —— 其余的量纲必须算出来, 而算的前提是每个变量
的量纲已知。U.5 正是那张表: | 符号 | 含义 | 量纲 | 命名空间 |。

命名空间是这张表的关键, 不是装饰
--------------------------------
U.5 自己用第一张表说明了歧义: 裸 ``R`` 在电气域是电阻、在可靠性域是可靠度
函数; ``L`` 在电气域是电感、在控制域是相位裕度下限。第二个 ``k`` 也不唯一:
包含因子在 ``M`` 命名空间是 ``[1]``, 下垂系数在 ``Q`` 是 ``[Ω]``。

所以解析键是 **(符号, 命名空间)** 而不是符号。公式自带命名空间 (由 ID 的域
字母与所在附录决定), 见 :func:`resolve`。

本模块**不猜**: 覆盖不到的符号、量纲写成「见各条」的、命名空间对不上的,
一律返回 ``None`` 并记进报告, 由 G1/G7 门禁处置。§18.10 注 2 的纪律是
「不可追溯的值记 UNKNOWN, 绝不猜」。

量纲记号必须自解析, 不能交给 pint
----------------------------------
U.5 写的是 ``[V]`` ``[K/W]`` ``[T]`` ``[T⁻¹]`` ``[A²·s]`` 这种方括号记号,
pint 直接吃会炸 (``'[V]' is not defined``)。更危险的是 ``[T]``:
**在 U.5 的记号里 T 是时间**, 而 pint 的 ``T`` 是**特斯拉**
(``mass·time⁻²·current⁻¹``)。若直接 ``unit_dimension("T")``, 每一条以
``[T]`` 标注的公式 (MTTF/MTTR/MTBF/失效率/热时间常数… 几十条) 都会拿到
一个「看起来合法」的错量纲 —— 不报错, 只是全部算错。故 :data:`SPEC_UNITS`
是显式手写并逐条核对过的映射, 不经 pint。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping

from ..solver.symbolic import DIMENSIONLESS, Dimension

__all__ = [
    "DIMENSION_COMPONENTS_ORDER",
    "SymbolEntry",
    "SymbolTable",
    "SymbolTableReport",
    "parse_spec_dimension",
    "parse_symbol_table",
]


# ---------------------------------------------------------------------------
# U.5 的量纲记号 -> 七维 Dimension
# ---------------------------------------------------------------------------

#: 基本量符号。U.5 的 ``[T]`` 单独处理 (见 :data:`_TIME_ALIAS`)。
_BASE = {
    "m": "length",
    "kg": "mass",
    "g": "mass",  # 这里只累计**指数**, 不累计量值 —— 克与千克都是质量
    "s": "time",
    "A": "current",
    "K": "temperature",
    "mol": "amount",
    "cd": "luminous_intensity",
}

#: 导出单位符号 -> 七维指数。由 SI 关系式推导, 逐条核对过。
_DERIVED = {
    "V": {"length": 2.0, "mass": 1.0, "time": -3.0, "current": -1.0},  # J/A
    "W": {"length": 2.0, "mass": 1.0, "time": -3.0},  # J/s
    "J": {"length": 2.0, "mass": 1.0, "time": -2.0},
    "N": {"length": 1.0, "mass": 1.0, "time": -2.0},
    "Ω": {"length": 2.0, "mass": 1.0, "time": -3.0, "current": -2.0},  # V/A
    "F": {"length": -2.0, "mass": -1.0, "time": 4.0, "current": 2.0},  # C/V
    "C": {"length": 2.0, "mass": 1.0, "time": -1.0, "current": 1.0},  # A*s
    "H": {"length": 2.0, "mass": 1.0, "time": -2.0, "current": -2.0},  # V*s/A
    "Wb": {"length": 2.0, "mass": 1.0, "time": -2.0, "current": -1.0},  # V*s
    "Hz": {"time": -1.0},
    "S": {"length": -2.0, "mass": -1.0, "time": 3.0, "current": 2.0},  # A/V
}

#: 无量纲记号。U.5 写「无量纲」「1」「—」「见各条」「视状态而定」等。
#: 真正无量纲的进这里; 其余 (``见各条`` 等) 返回 ``None``, 表示「本表未给」。
_DIMENSIONLESS_TOKENS = frozenset({"无量纲", "1", "—", "-", "°", "deg", "dB", "bit", "turn"})

#: U.5 用 ``T`` 表示**时间**, 与特斯拉同形。加这条别名是因为 ``[T]``/``[T⁻¹]``
#: 出现在 MTTF/MTTR/MTBF/失效率/热时间常数等几十条公式的量纲列里, 而
#: ``unit_dimension("T")`` 会返回特斯拉 —— 不报错, 只是全错。
_TIME_ALIAS = "time"

#: 无量纲但会出现在 ``[...]`` 里的词。``turn`` 是匝数 (磁阻 ``A·turn/Wb``
#: 里与 A 同量纲相乘), ``rad``/``°``/``dB``/``bit`` 同理。
_DIMENSIONLESS_SYMS = frozenset({"rad", "sr", "turn", "t", "n", "p"})

#: 上标数字 -> ASCII。U.5 写的是真上标字符: ``A²·s`` 的 ² 是 U+00B2,
#: ``T⁻¹`` 的 ⁻¹ 是 U+207B + U+00B9。它们**不是** ``[0-9]``, 不折算的话
#: 指数整段匹配不上, ``A²`` 被读成 ``A`` —— 错一整个幂次且不报错。
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")

#: 一个「符号 + 可选指数」的记号。
_TOKEN_RE = re.compile(r"([A-Za-zΩ]+)([+-]?[0-9]+(?:\.[0-9]+)?)?")


#: Dimension 的七个分量名 (与 solver.symbolic.DIMENSION_COMPONENTS 同序)。
DIMENSION_COMPONENTS_ORDER = (
    "length",
    "mass",
    "time",
    "current",
    "temperature",
    "amount",
    "luminous_intensity",
)


def _blank() -> dict[str, float]:
    return dict.fromkeys(DIMENSION_COMPONENTS_ORDER, 0.0)


def _parse_segment(seg: str, acc: dict[str, float]) -> bool:
    """把一段 (分子或分母) 的乘积累加进 ``acc``。认出不认识的单位返回 False。"""
    # 纯数字段是「无量纲的倍数」: ``[1]`` 就是无量纲, ``[1/h]`` 的分子就是 1。
    # 不特判的话 ``_TOKEN_RE`` 匹配不到任何记号, saw 保持 False, 整个 ``[1/h]``
    # 被判成「不认识的单位」而返回 None。
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", seg):
        return True
    saw = False
    for m in _TOKEN_RE.finditer(seg):
        sym, exp = m.group(1), m.group(2)
        power = float(exp) if exp else 1.0
        if sym in _DIMENSIONLESS_SYMS:
            saw = True
            continue
        if sym == "T":
            acc[_TIME_ALIAS] += power
        elif sym in _BASE:
            acc[_BASE[sym]] += power
        elif sym == "eV":  # 能量: 与 J 同量纲, 1.6e-19 的差是量值不进指数
            for comp, val in _DERIVED["J"].items():
                acc[comp] += val * power
        elif sym in _DERIVED:
            for comp, val in _DERIVED[sym].items():
                acc[comp] += val * power
        else:
            return False
        saw = True
    return saw


def parse_spec_dimension(text: str | None) -> Dimension | None:
    """U.5 的量纲记号 -> :class:`Dimension`。**无法确定时返回 ``None``**。

    返回 ``None`` 的三种情况, 都必须由门禁处置而不是猜:

    * ``None`` / 空 —— 该列没写
    * ``见各条`` / ``视状态而定`` / ``见附录`` —— 方案自己把定义推到了别处
    * 记号里有本表不认识的单位 —— 多半是 U.5 自身漏登记

    除号按「分子段累加、分母段减累加」处理: ``K/W`` 是温度/功率, 不是温度×功率。
    早先一版忽略除号, 于是 ``[K/W]`` 被读成 ``K·W`` —— 长度、质量、时间全错,
    而返回值完全合法, 没有任何报错。同理上标数字 (``A²`` 的 ² 是 U+00B2,
    ``T⁻¹`` 的 ⁻¹ 是 U+207B+U+00B9) 不是 ``[0-9]``, 不先折算则指数整段匹配不上,
    ``A²`` 被读成 ``A`` —— 量纲错一整个幂次。
    """
    if text is None:
        return None
    body = text.strip().strip("`").strip()
    if body.startswith("[") and body.endswith("]"):
        body = body[1:-1]
    body = body.strip()
    if body in _DIMENSIONLESS_TOKENS:
        return DIMENSIONLESS
    # 空格子是「方案没写」, 不是「无量纲」。两者混同会让没标注的符号
    # 静默获得无量纲 —— 而无量纲参与齐次性判定时常常「恰好通过」。
    if not body:
        return None
    # 「视状态而定」「见各条」这类不是量纲, 是「本表未给」
    if body.startswith(("见", "视")) or "各条" in body:
        return None

    body = body.translate(_SUPERSCRIPT).replace("·", "*").replace(" ", "")

    acc = _blank()
    # U.5 的记号无括号嵌套 (如 ``A/W·Wb``), 故按 '/' 分奇偶段即可
    for i, seg in enumerate(re.split(r"/", body)):
        if not seg:
            return None
        sign = -1.0 if i % 2 else 1.0
        sub = _blank()
        if not _parse_segment(seg, sub):
            return None
        for comp, val in sub.items():
            acc[comp] += sign * val
    return Dimension(**acc)


# ---------------------------------------------------------------------------
# 符号表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolEntry:
    """U.5 的一行。

    ``dimension`` 为 ``None`` 表示该行的量纲在本表里没定 (``见各条`` 等)。
    """

    symbol: str
    meaning: str
    dimension: Dimension | None
    dimension_text: str
    namespaces: tuple[str, ...]
    #: 该行的符号数与量纲数不等, 量纲是**整格套给全部符号**的, 不是按位对应。
    #:
    #: 真实例子: ``| `U`, `u_c`, `u_A`, `u_B`, `k` | … | `[X]`,`[1]` | `M` |``
    #: —— 5 个符号 2 个量纲。按位对应会把 ``k``(包含因子, 应为 ``[1]``) 判成
    #: ``[X]``(不确定度)。方案没给可机械判定的对应关系, 故不猜, 记此标志
    #: 并让 :attr:`dimension` 为 ``None``, 由门禁排除用到它的公式。
    ambiguous: bool = False

    @property
    def is_dimensionless(self) -> bool:
        return self.dimension is not None and self.dimension == DIMENSIONLESS


@dataclass
class SymbolTable:
    """按 ``(符号, 命名空间)`` 索引的符号字典。"""

    entries: tuple[SymbolEntry, ...] = ()
    #: 符号 -> 全部条目 (同一符号在不同命名空间含义不同)
    by_symbol: Mapping[str, tuple[SymbolEntry, ...]] = field(default_factory=dict)

    def namespaces_of(self, symbol: str) -> tuple[str, ...]:
        out: list[str] = []
        for e in self.by_symbol.get(symbol, ()):
            for ns in e.namespaces:
                if ns not in out:
                    out.append(ns)
        return tuple(out)

    def resolve(self, symbol: str, namespace: str | None) -> SymbolEntry | None:
        """解 ``(符号, 命名空间)`` -> 条目。解不出返回 ``None``。

        命名空间匹配规则: 精确相等, 或表里写的是公式所在**域的子命名空间**
        (U.5 用 ``E``/``K``/``N``/``J``/``M``/``Q``/``P``/``T``/``S`` 等字母,
        也写 ``W8`` ``L5`` ``W10`` ``W11`` 这类带号的)。故 ``J`` 命名空间的
        公式可以命中标着 ``E`` 的条目吗? **不可以** —— U.5 第一张表明确说
        ``R`` 在两个域里含义不同, 所以这里只做相等匹配, 由调用方决定回退策略。
        """
        cands = self.by_symbol.get(symbol, ())
        if not cands:
            return None
        if namespace is None:
            # 无命名空间: 仅当该符号在所有命名空间里量纲一致时才敢返回
            dims = {e.dimension for e in cands if e.dimension is not None}
            if len(cands) == 1 or len(dims) == 1:
                return cands[0]
            return None
        for e in cands:
            if namespace in e.namespaces:
                return e
        return None

    def resolve_dimension(self, symbol: str, namespace: str | None) -> Dimension | None:
        e = self.resolve(symbol, namespace)
        return e.dimension if e is not None else None


@dataclass
class SymbolTableReport:
    entries: int = 0
    rows: int = 0
    #: (行号, 原因, 原文)
    dropped: tuple[tuple[int, str, str], ...] = ()
    #: 量纲无法确定的条目 —— G7 的「符号表全覆盖」要看这个数
    unknown_dimension: tuple[str, ...] = ()
    #: 符号数与量纲数不等、只能整格套用的条目 (见 :attr:`SymbolEntry.ambiguous`)
    ambiguous: tuple[str, ...] = ()
    role_counts: Counter[str] = field(default_factory=Counter)

    def summary(self) -> str:
        lines = [
            f"符号条目: {self.entries} (来自 {self.rows} 行)",
            f"量纲已定: {self.entries - len(self.unknown_dimension) - len(self.ambiguous)}",
            f"量纲未定(方案推给别处, 如「见各条」): {len(self.unknown_dimension)}",
            f"符号/量纲个数不等(不猜): {len(self.ambiguous)}",
            f"丢弃行: {len(self.dropped)}",
        ]
        for reason, n in Counter(r for _ln, r, _f in self.dropped).most_common():
            lines.append(f"  {n} 行: {reason}")
        if self.ambiguous:
            lines.append(f"  个数不等样例: {', '.join(self.ambiguous[:8])}")
        if self.unknown_dimension:
            lines.append(f"  量纲未定样例: {', '.join(self.unknown_dimension[:8])}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

_U5_HEADING = re.compile(r"^###\s+U\.5\b")
_ANY_HEADING = re.compile(r"^#{1,4}\s+\S")
_SPLIT_LIST = re.compile(r"[,，]")


def _split_symbols(cell: str) -> list[str]:
    """把 ``V_in`, `V_out`, `V_ref`` 切成三个符号。

    顿号与斜杠也当分隔符: U.5 里有 ``DC`(诊断覆盖率), `PFH`…`` 这类写法。

    **去括号注释必须在去反引号之前。** ``DC`(诊断覆盖率)`` 先剥反引号会留下
    尾部的 ``)``, 正则去不掉, 再去反引号就得到 ``DC``` —— 字典里凭空多出
    一个带反引号的符号, 而 ``DC`` 本体查不到 (同理 ``D`(占空比)``、
    ``k`(下垂系数)``、``N`(中值窗长)``)。
    """
    out = []
    for p in re.split(r"[,，、/]", cell):
        s = re.sub(r"[（(].*?[)）]\s*$", "", p.strip()).strip()
        s = s.strip("`*").strip()
        if s:
            out.append(s)
    return out


def _split_dimension(cell: str) -> list[str]:
    return [d.strip() for d in _SPLIT_LIST.split(cell.strip().strip("`")) if d.strip()]


def _split_namespaces(cell: str) -> tuple[str, ...]:
    return tuple(n.strip() for n in _SPLIT_LIST.split(cell.strip().strip("`")) if n.strip())


def parse_symbol_table(lines: list[str]) -> tuple[SymbolTable, SymbolTableReport]:
    """抽 U.5 的正式符号表 (| 符号 | 含义 | 量纲 | 命名空间 |)。

    纯函数。U.5 的第一张表 (裸符号歧义说明) 不抽 —— 它是**说明**不是定义,
    量纲列在第二张表里才有。
    """
    start = None
    for i, line in enumerate(lines):
        if _U5_HEADING.match(line.strip()):
            start = i
            break
    if start is None:
        return SymbolTable(), SymbolTableReport(dropped=((0, "找不到 U.5 章节", ""),))

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _ANY_HEADING.match(lines[j].strip()):
            end = j
            break

    body = lines[start:end]

    entries: list[SymbolEntry] = []
    dropped: list[tuple[int, str, str]] = []
    rows = 0

    header: list[str] | None = None
    for offset, line in enumerate(body):
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(set(c) <= set(":- ") and "-" in c for c in cells):
            continue
        if header is None:
            # 第一张表 (裸符号歧义说明) 的表头不含「命名空间」⟹ 不是正式表
            if not any("命名空间" in c for c in cells):
                continue
            header = cells
            continue

        if len(cells) != len(header):
            dropped.append((start + offset + 1, "列数与表头不符", s[:60]))
            continue

        sym_cell = cells[0]
        meaning = cells[1]
        dim_cell = cells[2]
        ns_cell = cells[3]

        symbols = _split_symbols(sym_cell)
        if not symbols:
            dropped.append((start + offset + 1, "符号列为空", s[:60]))
            continue
        dims = _split_dimension(dim_cell)
        namespaces = _split_namespaces(ns_cell)
        rows += 1

        # 符号数与量纲数不等时不能按位对应 —— 例如
        # | `I_trip`, `U_trip`, `I_rel`, `U_rel` | ... | `[A]`,`[V]` | (4 vs 2)
        # 按位对应会把 I_trip/U_trip 都判成电流。这里只处理两种无歧义情形,
        # 其余按「整格量纲套给全部符号」并由 :data:`ambiguous` 记录。
        ambiguous = len(symbols) != len(dims) and len(dims) != 1

        for k, sym in enumerate(symbols):
            if len(dims) == len(symbols):
                dim_text = dims[k]
            else:
                dim_text = dim_cell.strip().strip("`")
            d = parse_spec_dimension(dim_text)
            entries.append(
                SymbolEntry(
                    symbol=sym,
                    meaning=meaning,
                    dimension=None if ambiguous else d,
                    dimension_text=dim_text,
                    namespaces=namespaces,
                    ambiguous=ambiguous,
                )
            )

    table = SymbolTable(
        entries=tuple(entries),
        by_symbol=_group(entries),
    )
    report = SymbolTableReport(
        entries=len(entries),
        rows=rows,
        dropped=tuple(dropped),
        unknown_dimension=tuple(
            sorted({e.symbol for e in entries if e.dimension is None and not e.ambiguous})
        ),
        ambiguous=tuple(sorted({e.symbol for e in entries if e.ambiguous})),
    )
    return table, report


def _group(entries: list[SymbolEntry]) -> Mapping[str, tuple[SymbolEntry, ...]]:
    acc: dict[str, list[SymbolEntry]] = {}
    for e in entries:
        acc.setdefault(e.symbol, []).append(e)
    return {k: tuple(v) for k, v in acc.items()}
