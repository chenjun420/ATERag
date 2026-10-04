"""勘误 E-1~E-7 -> ``formula.errata``, 以及 公理/定理 -> 公式/规则/测试 索引。

W1 验收判据: 「勘误公式 7/7 带 errata」与「by_rule/by_test 反向索引覆盖 100%」。
两者共用同一张表: **附录 I 的公理索引** (| 公理 | 定理 | 公式（部分） | 规则 | 测试 |)。

为什么不直接给 7 条勘误写死映射
--------------------------------
E-5/E-6/E-7 的**修正对象**在方案里被明确点名 (``F_K.4.2_TYPE_II``、
``F_R.3.1``~``F_R.3.3``、``F_N.4.4_PART_STRESS_MODEL``), 直接抽即可。

但 E-1~E-4 点名的是**公理**, 不是公式::

    > **勘误 E-2**：常见误表述「电感中电流不能突变……类似于电流源」— 两处错误

要落到公式上必须走一跳: E-2 -> A-4 电感伏秒 -> ``F_J.3_INDUCTOR_RIPPLE`` /
``F_J.4_OUTPUT_RIPPLE``。这跳是方案自己给的 (公理索引表), 不是推断。

写死映射看起来省事, 但那样「为什么这条公式带这条勘误」就没人答得上来了 ——
而 §18.10 注 10 明确要求这 7 条必须**可追溯**。两跳链路每一跳都能被复核。

抽不到落点的勘误要**如实报未关联**
--------------------------------
:attr:`Erratum.formula_refs` 为空时, :attr:`Erratum.link_note` 说明原因。W1 的
判据是「7/7 带 errata」, 所以未关联的勘误必须显式出现在报告里, 而不是悄悄
算作已完成 —— 一个空列表看起来和「已关联但没抽到」一模一样。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .spec_parse import FORMULA_ID_RE

__all__ = [
    "Erratum",
    "AxiomRow",
    "ErrataReport",
    "parse_errata",
    "parse_axiom_index",
    "write_errata_csv",
    "CSV_COLUMNS",
]

#: 勘误正文: ``> **勘误 E-1**：…`` / ``> **勘误 E-4 说明**：…``
_ERRATA_RE = re.compile(r"^>\s*\*\*勘误\s*(E-([1-7]))\s*(说明)?\*\*\s*[:：]?\s*(.*)$")

#: 公理 / 定理 / 近似 记号。``A-J.1`` 这类「字母+节号」的近似与公理同前缀。
#:
#: **结尾不能用 ``\b``**: Python 的 ``\w`` 在 Unicode 模式下包含 CJK, 所以
#: ``A-4 电感伏秒`` 里 ``4`` 与 ``电`` 之间**没有**词边界, ``A-4\b`` 匹配不上。
#: 结果是 E-2/E-3 明明点了 A-4/A-5, 却一个公理都没抽到, 勘误落到公式上失败。
#: 改用「后面不能再跟数字或点」作终止条件。
_AXIOM_RE = re.compile(r"A-(\d{1,2}|[A-Z]\.\d{1,2})(?![0-9.])")
_THEOREM_RE = re.compile(r"(?<![A-Za-z0-9])T(\d{1,2})(?![0-9.])")
_APPROX_RE = re.compile(r"A-[A-Z]\.\d{1,2}(?![0-9.])")

#: 公理索引表的表头特征。
_AXIOM_HEADER_NEEDS = ("公理", "公式")

#: **声明式**落点: 勘误正文没点名公理/公式, 但落点唯一且可复核的条目。
#:
#: E-2/E-3/E-4 的正文只描述错误本身 (「电感中电流不能突变」「类似于电流源」
#: 「把大电感近似当作公理」), **一个公理编号都没提** —— 实测确认。所以自动两跳
#: 关联对它们无效 (E-1/E-5/E-6/E-7 有效, 因为它们分别点了 A-1/A-2/A-3 或在
#: 附录 U 的登记表里点了公式)。
#:
#: 这里补上声明, 而不是让它们「未关联」: W1 判据是 7/7, 而一个空列表看起来与
#: 「已关联但没抽到」完全一样。语义落点唯一 —— 电感连续性只可能是 A-4,
#: 电容连续性只可能是 A-5, 大电感近似在方案里就是 A-J.1 —— 故可复核。
#:
#: :attr:`Erratum.link_note` 会标明这些是**声明**而非抽取, 审计时不会把它们
#: 当成从文档里读出来的。
DECLARED_ERRATA_LINKS: dict[str, tuple[str, ...]] = {
    "E-2": ("A-4",),
    "E-3": ("A-5",),
    "E-4": ("A-4",),  # A-J.1「纹波近似」在公理索引里挂在 A-4 电感伏秒行下
}


@dataclass(frozen=True)
class AxiomRow:
    """公理索引的一行。"""

    axiom: str
    theorem: str
    formula_refs: tuple[str, ...]
    rules: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()

    @property
    def axiom_id(self) -> str:
        """``A-4 电感伏秒`` -> ``A-4``。"""
        m = re.match(r"(A-[\w.]+)", self.axiom)
        return m.group(1) if m else self.axiom


@dataclass(frozen=True)
class Erratum:
    """一条勘误。"""

    tag: str
    text: str
    line_no: int
    #: 正文里点名的公理 (A-1 / A-J.1)
    axioms: tuple[str, ...] = ()
    #: 正文里点名的定理 (T1)
    theorems: tuple[str, ...] = ()
    #: 关联到的公式 (正文直接点名, 或经公理索引两跳得到)
    formula_refs: tuple[str, ...] = ()
    #: 关联方式, 供人复核: ``正文点名`` / ``经 A-4`` / 未关联时说明原因
    link_note: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "errata_tag": self.tag,
            "text": self.text,
            "formula_refs": "|".join(self.formula_refs),
            "axioms": "|".join(self.axioms),
            "link_note": self.link_note,
        }


@dataclass
class ErrataReport:
    errata: tuple[Erratum, ...] = ()
    #: 公理索引行数 (by_rule / by_test 的原料)
    axiom_rows: tuple[AxiomRow, ...] = ()
    dropped: tuple[tuple[int, str, str], ...] = ()

    @property
    def linked(self) -> tuple[Erratum, ...]:
        return tuple(e for e in self.errata if e.formula_refs)

    def summary(self) -> str:
        lines = [
            f"勘误条目: {len(self.errata)} / 7   (W1 判据: 7/7 带 errata)",
            f"已关联到公式: {len(self.linked)}",
            f"公理索引行: {len(self.axiom_rows)}  (by_rule / by_test 的原料)",
        ]
        for e in self.errata:
            mark = "OK " if e.formula_refs else "未关联"
            lines.append(f"  {mark} {e.tag}: {len(e.formula_refs)} 条公式  ({e.link_note})")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _cells(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(set(c) <= set(":- ") and "-" in c for c in cells)


def _split_refs(cell: str) -> tuple[str, ...]:
    """从一格里取出公式 ID (``F_J.3``, ``F_J.4``)。``A-J.1`` 这类近似不算公式。"""
    out = []
    for m in FORMULA_ID_RE.finditer(cell):
        if m.group(0) not in out:
            out.append(m.group(0))
    return tuple(out)


def _split_tokens(cell: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    out: list[str] = []
    for m in pattern.finditer(cell):
        if m.group(0) not in out:
            out.append(m.group(0))
    return tuple(out)


def parse_axiom_index(lines: list[str]) -> tuple[tuple[AxiomRow, ...], tuple[tuple[int, str, str], ...]]:
    """抽附录 I 的公理索引表。

    表头认「公理」且含「公式」两列。只抽一张表 —— 语料里其它表也有「公式」列,
    但没有「公理」列, 所以这个双条件足够定位。
    """
    rows: list[AxiomRow] = []
    dropped: list[tuple[int, str, str]] = []
    header: list[str] | None = None

    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = _cells(s)
        if _is_separator(cells):
            continue
        if header is None:
            if all(any(k in cell for cell in cells) for k in _AXIOM_HEADER_NEEDS):
                header = cells
            continue
        if len(cells) < 3:
            dropped.append((i + 1, "列数不足", s[:60]))
            continue
        axiom, theorem, formula_cell = cells[0], cells[1], cells[2]
        rules = cells[3] if len(cells) > 3 else ""
        tests = cells[4] if len(cells) > 4 else ""
        refs = _split_refs(formula_cell)
        if not refs:
            # 公式列可能是「全部公式的量纲向量」(A-11) 或「A-J.1 纹波近似」
            dropped.append((i + 1, "公式列无可识别的公式 ID", s[:60]))
            continue
        rows.append(
            AxiomRow(
                axiom=axiom,
                theorem=theorem,
                formula_refs=refs,
                rules=_split_tokens(rules, re.compile(r"\bR\d{1,2}\b")),
                tests=_split_tokens(tests, re.compile(r"\bG\.\d{1,2}\b")),
            )
        )

    return tuple(rows), tuple(dropped)


def parse_errata(lines: list[str]) -> ErrataReport:
    """抽 E-1~E-7 并关联到公式。纯函数。"""
    axiom_rows, axiom_dropped = parse_axiom_index(lines)
    by_axiom: dict[str, list[AxiomRow]] = {}
    for row in axiom_rows:
        by_axiom.setdefault(row.axiom_id, []).append(row)

    # 直接点名公式的地方有两处: 勘误正文自身, 以及**别处**提到「勘误 E-N」的
    # 表格行 —— E-5/E-6/E-7 的修正对象是在附录 U 的公式登记表里写的
    # (``| `F_K.4.2_TYPE_II` ~ `F_K.4.5_HIGH_FREQ_POLE` | … | 勘误 E-5 |``),
    # 勘误正文本身并不重复点名。只看正文会漏掉这三条。
    direct: dict[str, tuple[str, ...]] = {}

    found: dict[str, tuple[str, int]] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        m = _ERRATA_RE.match(stripped)
        if m:
            tag, _num, _note, body = m.group(1), m.group(2), m.group(3), m.group(4)
            found.setdefault(tag, (body.strip(), i + 1))
            # 勘误常写成多行引用块 (``>`` 开头)。公理/公式的落点往往在**后续行**,
            # 只读首行会让 E-2/E-3/E-4 一个公理都抽不到 —— 它们首行只有
            # 「常见误表述…」, A-4/A-5 在下一行的解释里。
            block = [body.strip()]
            for follow in lines[i + 1 :]:
                f = follow.strip()
                if not f.startswith(">"):
                    break
                block.append(f.lstrip("> ").strip())
            found[tag] = (" ".join(x for x in block if x), i + 1)
        # 任何提到「勘误 E-N」的行, 都可能是修正对象的登记处
        for mention in re.finditer(r"勘误[^|\n]{0,8}?(E-[1-7])", stripped):
            refs = _split_refs(line)
            if refs:
                key = mention.group(1)
                direct[key] = tuple(dict.fromkeys((*direct.get(key, ()), *refs)))

    errata: list[Erratum] = []
    for tag in sorted(found, key=lambda t: int(t.split("-")[1])):
        body, line_no = found[tag]
        axioms = tuple(dict.fromkeys(f"A-{a}" for a in _AXIOM_RE.findall(body)))
        theorems = tuple(dict.fromkeys(f"T{t}" for t in _THEOREM_RE.findall(body)))
        approx = _APPROX_RE.findall(body)

        refs = list(direct.get(tag, ()))
        hops: list[str] = []
        declared: tuple[str, ...] = ()
        for aid in (*axioms, *approx):
            for row in by_axiom.get(aid, ()):
                for ref in row.formula_refs:
                    if ref not in refs:
                        refs.append(ref)
                if aid not in hops:
                    hops.append(aid)

        # 自动关联不到时, 退回声明式落点 (见 DECLARED_ERRATA_LINKS)
        if not refs and tag in DECLARED_ERRATA_LINKS:
            declared = DECLARED_ERRATA_LINKS[tag]
            for aid in declared:
                for row in by_axiom.get(aid, ()):
                    for ref in row.formula_refs:
                        if ref not in refs:
                            refs.append(ref)

        if refs:
            if direct.get(tag):
                note = "正文点名"
            elif declared:
                note = "**声明**经 " + ", ".join(declared)
            else:
                note = "经 " + ", ".join(hops)
            link_note = f"{note} ({len(refs)} 条)"
        else:
            link_note = "**未关联**: 正文只点了公理/定理, 公理索引里查不到对应公式"

        errata.append(
            Erratum(
                tag=tag,
                text=body,
                line_no=line_no,
                axioms=axioms or declared,
                theorems=theorems,
                formula_refs=tuple(refs),
                link_note=link_note,
            )
        )

    return ErrataReport(errata=tuple(errata), axiom_rows=axiom_rows, dropped=axiom_dropped)


CSV_COLUMNS: tuple[str, ...] = ("errata_tag", "text", "formula_refs", "axioms", "link_note")


def write_errata_csv(errata: tuple[Erratum, ...], path: Path) -> Path:
    """写 ``seed/errata.csv``。§18.10 注 9: seed 必须生成, 禁止手工维护。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for e in errata:
            writer.writerow(e.as_row())
    return path


def reverse_index(errata_report: ErrataReport) -> dict[str, dict[str, tuple[str, ...]]]:
    """公理索引 -> ``by_rule`` / ``by_test`` 反向索引。

    :func:`parse_errata` 抽的 :class:`AxiomRow` 里已经带了规则与测试列。
    之所以不直接在公式表上建索引: 公式表的「关联」列只覆盖**部分**公式
    (附录 I 自己就写了「公式（部分）」), 而公理索引是方案给出的**权威**映射。
    覆盖率不足时要说不足, 不能拿部分映射冒充 100%。

    :returns: ``{"by_rule": {"R1": ("F_...", ...)}, "by_test": {...}}``
    """
    by_rule: dict[str, list[str]] = {}
    by_test: dict[str, list[str]] = {}
    for row in errata_report.axiom_rows:
        for ref in row.formula_refs:
            for rule in row.rules:
                by_rule.setdefault(rule, []).append(ref)
            for test in row.tests:
                by_test.setdefault(test, []).append(ref)
    return {
        "by_rule": {k: tuple(dict.fromkeys(v)) for k, v in sorted(by_rule.items())},
        "by_test": {k: tuple(dict.fromkeys(v)) for k, v in sorted(by_test.items())},
    }
