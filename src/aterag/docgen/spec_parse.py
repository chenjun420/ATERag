"""从方案 Markdown 里确定性地抽取公式行 (§18.3 公式知识库的输入)。

为什么不需要 LLM
----------------
方案把公式写成了**结构化表格**, 不是散文:

    | 公式 ID | 表达式 | 量纲 | 上游 |
    |---|---|---|---|
    | `F_J.2.1_BUCK` | `V_out = D · V_in` | `[V]` | A-2, A-4, T3 |

所以定位与结构化两步 (FormulaExtractor.locate / .structure) 都能用确定性
解析完成 —— §18.10 注 9「文档与代码同源」要求 ``seed/*.csv`` 由 ``docgen``
生成, 而可重现生成的前提正是没有 LLM 参与。

表结构不统一 —— 这是实测结论, 不是猜测
--------------------------------------
全库含 ``F_`` ID 的表格行共 846 行。列数从 2 到 10 都有, 占多数的表头是
``[公式 ID, 名称, 表达式, 上游]`` (205 张) 与 ``[公式 ID, 表达式, 上游]``
(147 张), 另有 ``[公式 ID, 表达式, 量纲]`` (44 张) 与列序不同的
``[公式 ID, 量纲, 表达式, 上游]`` (38 张)。所以**不能按列位取值**, 必须按
**列的角色**取值。

**每张公式表都有可识别的表头** (实测: 0 张缺失)。因此本模块只按表头取角色,
**不做任何内容猜测**。早先一版带「内容推断」兜底 (没表头时靠「含等号」「方括号
量纲」去猜), 结果把公式 ID 本身当成了表达式 —— 因为 ``F_J.2.1_BUCK`` 这类串
同样「含字母」。兜底路径已删: 猜错的代价是几百条公式带着错误 expression 进库,
而表头既然齐全, 兜底没有任何召回价值。

丢弃的行逐条记明原因 (:attr:`ParseReport.dropped`), 不静默丢。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

__all__ = [
    "FORMULA_ID_RE",
    "FormulaRow",
    "ParseReport",
    "parse_formula_rows",
    "read_spec",
]

#: 公式 ID 形态: ``F_<域>.<节>[.<子节>...]`` 后跟**可选**的 ``_<短名>``。
#:
#: 短名必须可选 —— 语料里两种写法都有: 带短名的
#: ``F_J.2.1_BUCK`` / ``F_M.3.4_CRC8_PEC``, 以及只到节号的
#: ``F_M.2.3`` / ``F_L.5.4`` / ``F_M.3.5`` (整节共用一条公式时这么写)。
#: 早先一版把短名写成必需, 于是这批 ID 一个都匹配不上, 对应的表格行被
#: 静默跳过 —— 表现为「抽出 545 条」而无任何报错。
#:
#: 末尾的 ``(?![A-Za-z0-9_.])`` 是必需的: 短名可选时, 引擎会先试长的再试短的,
#: 对 ``F_M.2.3_lowercase`` 这类畸形 ID 会截断匹配成 ``F_M.2`` —— 抽出一个
#: **存在但错误**的 ID, 比匹配失败危险得多 (它会去改别人的行)。
FORMULA_ID_RE = re.compile(r"\bF_[A-Z](?:\.[0-9]+)+(?:_[A-Z0-9_]+)?(?![A-Za-z0-9_.])")

#: 上游记号: ``A-2, A-4, T3`` / ``A-2；T3`` / ``—``
_UPSTREAM_TOKEN_RE = re.compile(r"^(?:A|I|T|E)-?[0-9]+(?:\.[0-9]+)*$")

_HEADING_RE = re.compile(r"^(#{2,4})\s+(.+?)\s*$")


# ---------------------------------------------------------------------------
# 列角色
# ---------------------------------------------------------------------------

#: 列角色的判定关键词。顺序有意义: 先匹配到的角色胜出, 所以更具体的放前面。
#:
#: 「公式」既可能指表达式列也可能指 ID 列, 故 ID 的关键词必须排在它前面 ——
#: ``公式 ID`` 与 ``表达式`` 都是 3 个字符, 靠位置区分比靠关键词可靠。
_ROLE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("formula_id", ("公式 ID", "公式ID", "公式编号", "公式 编号", "ID")),
    ("dimension", ("量纲", "单位", "量纲/单位")),
    ("expression", ("表达式", "计算式", "公式", "式", "表达式/形式")),
    ("upstream", ("上游", "依据", "公理", "定理", "推导自", "来源")),
    ("source", ("出处", "标准", "引用", "条款")),
    ("errata", ("勘误", "纠错", "E-")),
    ("name", ("名称", "名字", "中文名", "标题")),
    ("note", ("说明", "备注", "含义", "描述", "用途", "关联", "工程")),
)


@dataclass(frozen=True)
class FormulaRow:
    """一条从方案表格里抽出的公式。

    抽不出的字段一律为 ``None``, **不用默认值填充** —— 填充值会被下游当成
    「方案里写了但是我没读到」, 而真实情况是「方案里就没写」, 二者的补救
    动作不同。
    """

    formula_id: str
    expression: str | None = None
    dimension_text: str | None = None
    upstream: tuple[str, ...] = ()
    name_zh: str | None = None
    section: str | None = None
    domain: str | None = None
    errata: str | None = None
    source_ref: str | None = None
    line_no: int = 0

    @property
    def is_complete(self) -> bool:
        """够不够走 G1 门禁 —— 至少要有表达式。"""
        return self.expression is not None


@dataclass
class ParseReport:
    """解析覆盖报告。

    存在的理由: 抽取器的正确性判据不是「跑完不报错」, 而是「抽出的条数与
    丢掉的条数都可解释」。:attr:`dropped` 逐条记明原因, 供人工复核。
    """

    rows: tuple[FormulaRow, ...] = ()
    dropped: tuple[tuple[int, str, str], ...] = ()  # (行号, 原因, 原文片段)
    table_count: int = 0
    #: 含 ``F_`` ID 但表头认不出「公式 ID」列的表数 —— 整表不用, 不猜。
    unusable_tables: int = 0
    role_counts: Counter[str] = field(default_factory=Counter)
    #: 在多张表里出现过的公式 ID(被合并过)。构造 ParseReport 时转 frozenset。
    multi_table_ids: frozenset[str] = frozenset()

    @property
    def duplicate_ids(self) -> tuple[str, ...]:
        """在**多张表**里出现过的公式 ID(即被合并过的)。

        早先一版按「rows 里同一个 ID 出现多次」算重复, 但按字段合并之后每个
        ID 只留一行, 这个属性就恒为空 —— 信号丢了。改成统计「合并过的 ID」:
        它们正是靠附录 U 登记表才拿到 ``name_zh``/``upstream`` 的那批, 值得
        被看见(实测 29 个 ID 只存在于登记表)。
        """
        return tuple(sorted(self.multi_table_ids))

    def summary(self) -> str:
        lines = [
            f"公式表: {self.table_count} (表头认不出公式ID列而整表不用的: {self.unusable_tables})",
            f"抽出公式行: {len(self.rows)} (互不重复 ID: {len({r.formula_id for r in self.rows})})",
            f"有表达式: {sum(1 for r in self.rows if r.expression)}",
            f"有量纲记号: {sum(1 for r in self.rows if r.dimension_text)}",
            f"有上游: {sum(1 for r in self.rows if r.upstream)}",
            f"重复 ID: {len(self.duplicate_ids)}",
            f"丢弃行: {len(self.dropped)}",
        ]
        if self.duplicate_ids:
            lines.append(f"  重复样例: {', '.join(self.duplicate_ids[:6])}")
        reasons = Counter(r for _ln, r, _frag in self.dropped)
        for reason, n in reasons.most_common():
            lines.append(f"  丢弃 {n} 行: {reason}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def read_spec(path: Path) -> list[str]:
    """读方案文件, 返回按行切分的列表 (保留空行, 便于按行号定位)。"""
    return path.read_text(encoding="utf-8").splitlines()


def _cells(line: str) -> list[str]:
    """切分 Markdown 表格行。转义竖线 ``\\|`` 不切 —— 公式里罕见但有可能。"""
    s = line.strip()
    if not s.startswith("|"):
        return []
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)[1:-1]]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(set(c) <= set(":- ") and "-" in c for c in cells)


def _is_table_start(line: str) -> bool:
    """是不是表格的第一行。

    判据是「以竖线开头**且**不是分隔行」—— 分隔行 (``|---|---|``) 也以竖线
    开头, 但它不开启一张表。
    """
    cells = _cells(line)
    return bool(cells) and not _is_separator(cells)


def _classify_header(cells: list[str]) -> dict[str, int]:
    """把表头映射成 ``{角色: 列下标}``。

    匹配不到任何已知角色的表头返回空 dict —— 调用方据此退回内容推断。
    """
    out: dict[str, int] = {}
    for i, cell in enumerate(cells):
        text = cell.strip().strip("`* ")
        for role, kws in _ROLE_KEYWORDS:
            if role in out:
                continue
            if any(k in text for k in kws):
                out[role] = i
                break
    return out


def _domain_of(formula_id: str, section: str | None) -> str | None:
    """域字母: 取自 ID 的第二段 (``F_J.2.1_X`` -> ``J``)。

    注意 ID 里域字母后面**直接是点号** (``F_J.2.1``), 没有数字, 所以判据是
    「字母后紧跟 ``.`` 或 ``_``」而不是「字母后跟数字」。
    """
    m = re.match(r"F_([A-Z])(?=[._])", formula_id)
    if m:
        return m.group(1)
    if section:
        m2 = re.match(r"([A-Z])[0-9]+(?:\.[0-9]+)*", section)
        if m2:
            return m2.group(1)
    return None


#: 「同上」类占位词。附录 U.2.2 的公式登记表大量用它表示「上游与上一行相同」。
_SAME_AS = frozenset({"同上", "同上。", "同前", "同左"})


def _can_fill(picked: Mapping[str, str], slot: FormulaRow) -> bool:
    """这一行能否给已存在的 ``slot`` 补上它缺的字段。"""
    return bool(
        (picked.get("expression") and slot.expression is None)
        or (picked.get("name") and slot.name_zh is None)
        or (picked.get("dimension") and slot.dimension_text is None)
        or (picked.get("source") and slot.source_ref is None)
        or (slot.section is None)
        or (slot.domain is None)
    )


def parse_formula_rows(lines: list[str]) -> ParseReport:
    """抽出全部公式行。纯函数, 不碰文件系统与数据库。"""
    rows: list[FormulaRow] = []
    dropped: list[tuple[int, str, str]] = []
    role_counts: Counter[str] = Counter()
    #: 运行期累加: 真正从别的表补到过字段的 ID(局部变量, 非 dataclass 字段)。
    merged_seen: set[str] = set()
    table_count = 0
    unusable_tables = 0
    #: formula_id -> 已合并的行。按字段合并(见下方注释), 不再是「首次即定」。
    merged: dict[str, FormulaRow] = {}
    #: 首次出现顺序, 保证输出稳定。
    order: list[str] = []
    #: 本表上一行的上游原文, 供「同上」沿用。
    last_upstream = ""

    section: str | None = None
    domain: str | None = None
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        hm = _HEADING_RE.match(line.strip())
        if hm:
            section = hm.group(2).strip()
            m2 = re.match(r"([A-Z])[0-9]+(?:\.[0-9]+)*", section)
            domain = m2.group(1) if m2 else domain
            i += 1
            continue

        if not _is_table_start(line):
            i += 1
            continue

        # 收集整张表
        block: list[tuple[int, list[str]]] = []
        while i < n:
            cells = _cells(lines[i])
            if not cells:
                break
            block.append((i + 1, cells))
            i += 1
        block = [(ln, c) for ln, c in block if not _is_separator(c)]
        if not block:
            continue

        has_formula_id = any(FORMULA_ID_RE.search(" ".join(c)) for _ln, c in block)
        if not has_formula_id:
            continue
        table_count += 1

        header: dict[str, int] = {}
        if not FORMULA_ID_RE.search(" ".join(block[0][1])):
            header = _classify_header(block[0][1])
            body = block[1:]
        else:
            body = block

        # 表头必须认得出「公式 ID」列, 否则整表不用。
        #
        # 为什么不能用内容兜底找 ID: 表头「公式与规则」「公式」这类列名会被
        # 判成 expression 列 (「公式」是 expression 的关键词), 于是**公式 ID
        # 本身被当成表达式** —— 实测里 ``F_L.1_ALARM_MARGIN`` 的 expression
        # 就是它自己。行内按 ``F_[A-Z]N.N_X`` 找 ID 是安全的 (该形态无歧义),
        # 但**表头角色判定**不是, 所以这里不放行。
        if "formula_id" not in header:
            unusable_tables += 1
            continue

        for ln, cells in body:
            # 一格里可能有**多个**公式 ID。附录 U 的登记表把同源公式并成一格:
            #   | `F_J.7.1_RESONANT_FREQ` / `F_J.7.3_LLC_FREQ` | 谐振频率 | ... |
            # 早先只取第一个 (FORMULA_ID_RE.search + break), 于是同格的后半截
            # ID 永远拿不到中文名 —— 实测这是剩余 name_zh 缺口的主要来源。
            #
            # 只在**公式 ID 列**上做多 ID 展开: 在任意格里 finditer 会把
            # 「上游」列里的公理号或表达式里的字符串误当公式 ID。
            id_idx = header.get("formula_id")
            scan_cells = [cells[id_idx]] if id_idx is not None and id_idx < len(cells) else cells
            ids: list[str] = []
            for c in scan_cells:
                ids.extend(m.group(0) for m in FORMULA_ID_RE.finditer(c))
            if not ids:
                continue
            # 去重但保持顺序(同一 ID 在一格里出现两次无意义)。
            ids = list(dict.fromkeys(ids))
            fid = ids[0]

            picked: dict[str, str] = {}
            for role, idx in header.items():
                if idx < len(cells) and cells[idx]:
                    picked.setdefault(role, cells[idx].strip().strip("`*"))

            for role in picked:
                role_counts[role] += 1

            expr = picked.get("expression")
            if expr and FORMULA_ID_RE.search(expr):
                # 兜底不变式: 表达式格绝不能是公式 ID 本身。
                # 表头角色判定可能把 ID 列误判成表达式列 (见上), 那一类行必须
                # 在这里被挡住 —— 否则它们会以「expression = 自己的 ID」进库,
                # 而下游的量纲校验对纯 ID 串必然判不齐, 报错信息还会指向量纲引擎。
                dropped.append((ln, "表达式列取到的是公式ID本身(表头角色误判)", fid))
                continue

            upstream_raw = picked.get("upstream", "")
            # 「同上」= 沿用本表上一行的上游。附录 U.2.2 的登记表大量使用它,
            # 不展开就等于丢掉该表整章的上游信息。
            if upstream_raw in _SAME_AS and last_upstream:
                upstream_raw = last_upstream
            upstream = tuple(
                p.strip()
                for p in re.split(r"[,，;；]", upstream_raw)
                if p.strip() and _UPSTREAM_TOKEN_RE.match(p.strip())
            )
            if upstream:
                last_upstream = upstream_raw

            # 无表达式的行**不是废行**。附录 U 的登记表
            # (公式 ID | 名称 | 上游 | 规则 | 测试) 根本没有表达式列, 却带着
            # 中文名与上游。早先一版在这里整行丢弃, 于是附录 U 对抽取**零贡献**
            # —— 实测 83 条闭合公式因此缺 name_zh, 而那些中文名在方案里**明明
            # 写着**(F_J.5.1_REFLECTED_VOLTAGE -> 「反射电压」)。这不是方案缺口,
            # 是抽取器把登记表丢了。故改为**按字段合并**。
            #
            # 合并规则: 表达式只认**第一个**非空值(公式表才是权威), 其余字段
            # 谁有算谁的 —— 登记表出现得比公式表晚, 但它带的名称/上游正是
            # 公式表缺的那部分。
            for target in ids:
                multi_table = False
                if target not in merged:
                    # 顺序只在真正合并时登记 —— 上面那条「表达式列取到公式ID」
                    # 的分支会 continue, 提前登记 order 会指向不存在的条目。
                    order.append(target)
                elif _can_fill(picked, merged[target]):
                    # 该 ID 已在别的表里出现过, 且这次能补上它缺的字段。
                    multi_table = True
                slot = merged.setdefault(target, FormulaRow(formula_id=target))
                # ``FormulaRow`` 是 frozen 的, 故用 replace 生成新实例而非原地改。
                updates: dict[str, object] = {}
                if expr and slot.expression is None:
                    updates["expression"] = expr
                if picked.get("name") and slot.name_zh is None:
                    updates["name_zh"] = picked["name"]
                if upstream and not slot.upstream:
                    updates["upstream"] = upstream
                if picked.get("dimension") and slot.dimension_text is None:
                    updates["dimension_text"] = picked["dimension"]
                if picked.get("errata") and slot.errata is None:
                    updates["errata"] = picked["errata"]
                if picked.get("source") and slot.source_ref is None:
                    updates["source_ref"] = picked["source"]
                if slot.section is None:
                    updates["section"] = section
                if slot.domain is None:
                    updates["domain"] = _domain_of(target, section)
                if slot.line_no == 0:
                    updates["line_no"] = ln
                if updates:
                    merged[target] = replace(slot, **updates)  # type: ignore[arg-type]
                    if multi_table:
                        merged_seen.add(target)

    # 按首次出现顺序输出 —— 顺序变了会让「上游先于下游」的判断乱掉。
    for fid in order:
        row = merged[fid]
        # 空壳行: 公式 ID 出现过, 但既没表达式也没任何其它字段。这样的行
        # 对下游毫无用处, 却会让「抽出 N 条」虚高。丢掉并记明原因。
        if (
            row.expression is None
            and not (row.name_zh or row.upstream or row.dimension_text)
        ):
            dropped.append((row.line_no, "该 ID 未带任何可用字段(空壳行)", fid))
            continue
        rows.append(row)

    return ParseReport(
        rows=tuple(rows),
        dropped=tuple(dropped),
        table_count=table_count,
        unusable_tables=unusable_tables,
        role_counts=role_counts,
        multi_table_ids=frozenset(merged_seen),
    )
