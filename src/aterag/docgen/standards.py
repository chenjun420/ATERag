"""附录 V -> ``standards_registry`` 记录 (§18.3.1) 与 seed CSV。

W1 验收判据之一是「标准索引 ≥ 60 条」。附录 V 的 V.1~V.9 就是九张标准表,
结构一致: ``| 标准号 | 年份 | 名称 | 适用范围 | 关联规则/测试 |``。

引用状态一律 UNVERIFIED —— 这是方案自己定的规矩
------------------------------------------------
V.0「强制规范纪律」第四条: 「引用无法在标准库中核验时, 只保留标准号/名称并把
``citation_status`` 标 UNVERIFIED」。而本模块**只从文档里抽文本**, 没有连任何
标准库做过核验, 所以 UNVERIFIED 是唯一诚实的初值。

把「已核验」当成默认值会怎样: 下游看到 ``VERIFIED`` 就直接引用, 而那个标记
是我们自己编的 —— 于是「文档里写了这个标准号」被当成「这个标准号真实存在且
版本正确」。前者是我们能保证的, 后者不能。

只抽标准号与年份, 不抽「合规判定」
--------------------------------
「适用范围」列是人写的散文, 不构成机器可判定的条款号。所以本模块只把
``关联规则/测试`` 列里形如 ``R10``/``G.12``/``E.9``/``F.9``/``A-7``/``T20`` 的
记号取出来做反向索引的原料, 不去判断「本公式是否满足该标准条款」—— 那是 W2
确定性层的事, 这里只负责索引完整性。
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

__all__ = ["StandardRecord", "StandardsReport", "parse_standards", "write_standards_csv"]

#: 附录 V 的小节范围。V.0 是纪律, V.10/V.11 是出处映射, V.12 是版本管理 ——
#: 它们都不是标准表, 抽进来会污染索引。
_STANDARD_SECTIONS = ("V.1", "V.2", "V.3", "V.4", "V.5", "V.6", "V.7", "V.8", "V.9")

_APPENDIX_HEADING = re.compile(r"^##\s+附录\s*V\b")
_ANY_H2 = re.compile(r"^##\s+\S")
_SECTION_HEADING = re.compile(r"^###\s+(V\.\d+)\s*(.*)$")

#: 首列「是不是标准号」的判据是**结构性的**, 不是枚举标准组织。
#:
#: 早先一版枚举了 IEC/IEEE/ISO/GB/SJ/CISPR/SAE/UL…… 结果 82 条**真标准**被丢掉:
#: JEDEC JESD22-A104、ANSI/IEEE C37.112、DL/T 587、GB 9706.1、EOL 电阻……
#: 枚举标准组织永远会漏 —— 语料里有十几个来源, 以后还会更多。
#:
#: 结构性判据「含数字且不含中文」既宽松又不漏, 也不会把散文行放进来。
_HAS_DIGIT_RE = re.compile(r"\d")
_HAS_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

#: 反向索引记号: R10 / G.12 / E.9 / F.9 / A-7 / T20 / P3
_BINDING_RE = re.compile(r"\b(?:R\d{1,2}|G\.\d{1,2}|[A-Z]\.\d{1,2}|[A-Z]-\d{1,2})\b")

_FORMULA_ID_RE = re.compile(r"\bF_[A-Z](?:\.[0-9]+)+(?:_[A-Z0-9_]+)?(?![A-Za-z0-9_.])")

_EM_DASHES = ("—", "-", "–", "N/A", "n/a", "")


@dataclass(frozen=True)
class StandardRecord:
    """``standards_registry`` 的一行。"""

    standard_id: str
    version: str | None
    title: str
    scope: str | None
    domain: str
    #: 关联规则/测试记号 (R10/G.12/E.9/F.9/A-7/T20)
    bindings: tuple[str, ...] = ()
    #: 关联公式 ID (若关联列里写了 ``F_*``)
    formula_refs: tuple[str, ...] = ()
    citation_status: str = "UNVERIFIED"
    #: 标识里是否含**编号**。PMBus / CAN FD 这类以名称标识的规范为 False。
    #: 「是真标准但没编号」与「不是标准」是两回事, 必须能区分。
    has_number: bool = True

    def as_row(self) -> dict[str, str]:
        """CSV 一行。列表字段用 ``|`` 连接 —— 不用逗号, 免得再套一层引号。"""
        return {
            "standard_id": self.standard_id,
            "version": self.version or "",
            "title": self.title,
            "scope": self.scope or "",
            "domain": self.domain,
            "bindings": "|".join(self.bindings),
            "formula_refs": "|".join(self.formula_refs),
            "citation_status": self.citation_status,
            "has_number": "true" if self.has_number else "false",
        }


@dataclass
class StandardsReport:
    records: tuple[StandardRecord, ...] = ()
    #: (行号, 原因, 原文)
    dropped: tuple[tuple[int, str, str], ...] = ()
    sections_seen: tuple[str, ...] = ()

    @property
    def with_bindings(self) -> int:
        return sum(1 for r in self.records if r.bindings or r.formula_refs)

    def summary(self) -> str:
        lines = [
            f"标准条目: {len(self.records)}  (W1 验收门槛 >= 60)",
            f"带关联记号: {self.with_bindings}",
            f"覆盖小节: {', '.join(self.sections_seen)}",
            f"丢弃行: {len(self.dropped)}",
        ]
        for reason, n in Counter(r for _ln, r, _f in self.dropped).most_common():
            lines.append(f"  {n} 行: {reason}")
        return "\n".join(lines)


def _looks_like_standard_id(cell: str) -> bool:
    """首列是否是一个标准/规范的标识。

    刻意**不做**「拆斜杠」——
    ``GB 9706.1 / GB/T 16935``、``IEC 61869-5 / -6``、``IEC 60898-1/2`` 这类
    一格里含两个标准 (第二个有时还是简写)。硬拆会造出 ``2``、``-6`` 这种
    凭空捏造的标准号, 而 ``standards_registry`` 的键必须能追溯回文档原文。
    复合标准号就原样存着。

    也刻意**不要求含数字**: 有一批规范是以**名称**标识的 (PMBus
    Specification、SMBus、CAN FD、Modbus Application Protocol、IRIG Standard B),
    它们是真标准, 只是没有编号。要求含数字会把 47 条真规范判成散文。
    代价是判据变宽, 所以: (a) 只在 V.1~V.9 这些**本身就是标准表**的小节里用,
    靠小节范围兜住语义; (b) 是否含编号单独记在 :attr:`StandardRecord.has_number`
    上, 下游要按编号引用时能看见。
    """
    if not cell or len(cell) > 60:
        return False
    return not any(p in cell for p in ("。", "；", ";", "|", "："))


def _merge(records: list[StandardRecord]) -> tuple[StandardRecord, ...]:
    """按 ``standard_id`` 合并重复条目。

    同一个标准出现在多个小节是**正常的** (``IEC 60529`` 防尘防水在 V.1 安规、
    V.7 环境、电气规格三处都出现)。而 ``standards_registry`` 以标准号为键,
    不合并就会撞主键。

    合并规则: 版本取**已知的那个** (``—`` 不算版本, 但也不是「没有版本」——
    拿它去覆盖一个真版本才是丢信息); 域与关联记号取并集, 保持首次出现顺序。
    """
    order: list[str] = []
    by_id: dict[str, StandardRecord] = {}
    for rec in records:
        prev = by_id.get(rec.standard_id)
        if prev is None:
            order.append(rec.standard_id)
            by_id[rec.standard_id] = rec
            continue
        by_id[rec.standard_id] = _merge_two(prev, rec)
    return tuple(by_id[k] for k in order)


def _merge_two(a: StandardRecord, b: StandardRecord) -> StandardRecord:
    return StandardRecord(
        standard_id=a.standard_id,
        version=a.version or b.version,
        title=a.title or b.title,
        scope=a.scope or b.scope,
        domain=a.domain if a.domain == b.domain else f"{a.domain}; {b.domain}",
        bindings=tuple(dict.fromkeys((*a.bindings, *b.bindings))),
        formula_refs=tuple(dict.fromkeys((*a.formula_refs, *b.formula_refs))),
        citation_status=a.citation_status,
        has_number=a.has_number or b.has_number,
    )


def _clean_cell(cell: str) -> str:
    """去掉 Markdown 强调与反引号。表格里的标准号常写成 ``**IEC 60664-1**``。"""
    return cell.strip().strip("*` ").strip()


def _clean_version(raw: str) -> str | None:
    v = _clean_cell(raw)
    return None if v in _EM_DASHES else v


def _section_slice(lines: list[str]) -> tuple[int, int]:
    """附录 V 在文件中的行范围。"""
    start = next((i for i, line in enumerate(lines) if _APPENDIX_HEADING.match(line.strip())), None)
    if start is None:
        return -1, -1
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _ANY_H2.match(lines[j].strip()):
            end = j
            break
    return start, end


def parse_standards(lines: list[str]) -> StandardsReport:
    """抽附录 V.1~V.9 的标准条目。纯函数。"""
    start, end = _section_slice(lines)
    if start < 0:
        return StandardsReport(dropped=((0, "找不到附录 V", ""),))

    records: list[StandardRecord] = []
    dropped: list[tuple[int, str, str]] = []
    seen_sections: list[str] = []

    domain: str | None = None
    domain_title = ""
    header: list[str] | None = None
    in_scope = False

    for offset, line in enumerate(lines[start:end]):
        raw = lines[start + offset]
        s = raw.strip()

        m = _SECTION_HEADING.match(s)
        if m:
            domain, domain_title = m.group(1), m.group(2).strip()
            in_scope = domain in _STANDARD_SECTIONS
            header = None
            if in_scope:
                seen_sections.append(domain)
            continue

        if not in_scope or not s.startswith("|"):
            continue

        cells = [_clean_cell(c) for c in re.split(r"(?<!\\)\|", s.strip("|"))]
        if cells and all(set(c) <= set(":- ") and "-" in c for c in cells):
            continue  # 分隔行

        if header is None:
            # 表头必须含「标准号」, 否则这不是标准表 (V.10/V.11 的表长这样)
            if not any("标准" in c for c in cells):
                continue
            header = cells
            continue

        std_id = cells[0]
        if not _looks_like_standard_id(std_id):
            dropped.append((start + offset + 1, "首列不像标准号", s[:60]))
            continue

        # 列数不足时按缺列补空 —— 宁可少填一个可选字段, 不猜内容
        def cell(idx: int) -> str:  # noqa: ANN202
            return cells[idx] if idx < len(cells) else ""

        record = StandardRecord(
            standard_id=std_id,
            version=_clean_version(cell(1)),
            title=cell(2),
            scope=cell(3) or None,
            domain=f"{domain} {domain_title}".strip(),
            bindings=tuple(dict.fromkeys(_BINDING_RE.findall(cell(4)))),
            formula_refs=tuple(dict.fromkeys(_FORMULA_ID_RE.findall(cell(4)))),
            has_number=bool(_HAS_DIGIT_RE.search(std_id)),
        )
        if not record.title:
            dropped.append((start + offset + 1, "名称列为空", s[:60]))
            continue
        records.append(record)

    return StandardsReport(
        records=_merge(records),
        dropped=tuple(dropped),
        sections_seen=tuple(seen_sections),
    )


#: CSV 列序。显式列出而不是用 dict 的插入序 —— 列序是 seed 文件的接口。
CSV_COLUMNS: tuple[str, ...] = (
    "standard_id",
    "version",
    "title",
    "scope",
    "domain",
    "bindings",
    "formula_refs",
    "citation_status",
    "has_number",
)


def write_standards_csv(records: tuple[StandardRecord, ...], path: Path) -> Path:
    """写 ``seed/standards.csv``。

    §18.10 注 9: ``seed/*.csv`` 必须由 ``docgen`` 生成, **禁止手工维护** ——
    手工维护的 seed 迟早与方案脱节, 而门禁比对的是方案。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for rec in records:
            writer.writerow(rec.as_row())
    return path
