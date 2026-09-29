"""缝② 条目剔除: 等级/备注命中剔除词表的条目.

关键语义区分 (需求方口径): 表格里的短横线 `-` 表示"该维度不存在数据或不要求",
但它与"等级列说不要求"是**两件不同的事**, 混为一谈会误删有效条目:

  等级=不要求        整条条目不做要求  -> 剔除 (R1)
  单元格 = "-"       该维度无数据/无要求, 同一行其他维度可能仍有要求 -> 保留,
                     只把该维度记为"无数据", 不参与条件装配

真实数据依据 (PA601, 等级=强制 的行里 unit 列为 "-" 的有 8 行):
  SR-1101 输入工作电压范围  Vdc 档: 限值全 "-", 但同编号 Vac 档 88~290Vac 有效
  SR-1217 温度系数          3.45V 轨: "-", 但 -54V 轨 ±0.02%/℃ 有效
  SR-1219 负载均流度        min="-" 但 max=50%, 数值部分有效
若把 `-` 当成"不要求"剔除, 上面这些行会被整条删掉 —— 属误删。

规则 (不可改成子串匹配, 见 R3):

  R1 等级剔除   priority 精确命中剔除词        SR-1107/1108/1202/1224 (等级列=不要求)
  R2 备注整格   notes 精确命中剔除词          SR-1224 (备注=不要求), 与 R1 互为双保险
  R3 子串保护   "不要求"出现在备注里 ≠ 该条目不要
                SR-1219 负载均流度: 等级=强制, 备注写"不要求均流度, 但不能出现……"
                -> 包含式匹配会误删强制项, 故备注只做整格等值判定
  R4 等级缺失   priority 为空 -> 保留并打 priority_unclassified 标记
                缺失不伪装成任何一种结论: 既不静默丢, 也不静默收
  R5 短横线     单元格 `-` 是"无数据", 不是"不要求" -> 保留该行, 仅标注

每一次剔除都产出一条 ExcludedItem (带原因与命中词), 剔除行为本身可审计。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aterag.extract.models import ExcludedItem

_EMPTY = {"", "-", "—", "/"}


def _norm(v: Any) -> str:
    return str(v or "").strip().strip("。.").strip()


def is_placeholder(v: Any) -> bool:
    """单元格是否为"无数据"占位符 (短横线/空/斜杠)。

    语义: 该维度不存在数据或不要求, 但**不代表整条条目不要求** (见模块 docstring R5)。
    """
    return str(v or "").strip() in _EMPTY


# 表格专属列: 只有当同表的其他专属列有值时, 它们的 "-" 才算"无数据";
# 否则只是"这张表本来就没这列"。
_TABLE_SPECIFIC = ("subject", "signal_name", "signal_req", "range_text", "accuracy")
# 电压轨/单位列: 遥测表这类"无电压轨维度"的表里, 空值是表结构使然而非数据缺失;
# 参数表里空值才真的表示"该条目不分轨"。
_TIERED_COLS = ("rail", "unit")
_LIMIT_COLS = ("min", "typ", "max")


def _is_tiered_row(row: Mapping[str, Any]) -> bool:
    """该行是否来自"无电压轨维度"的表 (遥测表: 有 range_text/subject, 无 min/typ/max)。"""
    has_specific = any(
        not is_placeholder(row.get(f))
        for f in ("subject", "range_text", "signal_req", "signal_name")
    )
    has_limits = any(not is_placeholder(row.get(f)) for f in _LIMIT_COLS)
    return has_specific and not has_limits


def no_data_dimensions(row: Mapping[str, Any]) -> list[str]:
    """列出该行处于"无数据"状态的维度 (供输出可见, 不做剔除)。

    只报告"该表确实有此列、但没给数"的维度, 否则统计会被表结构差异污染:
    遥测表没有 rail 列, 其 rail 为空是表结构使然, 不是数据缺失。
    """
    specific = {f: row.get(f) for f in _TABLE_SPECIFIC}
    table_has_specific = any(not is_placeholder(v) for v in specific.values())
    tiered = _is_tiered_row(row)
    out: list[str] = []
    for f, v in row.items():
        if f in {"model_id", "parent_headings", "priority"}:
            continue
        if f in _TABLE_SPECIFIC and not table_has_specific:
            continue
        if f in _TIERED_COLS and tiered:
            continue
        if is_placeholder(v):
            out.append(f)
    return out


@dataclass
class SieveOutcome:
    kept: list[dict[str, Any]]
    excluded: list[ExcludedItem]
    unclassified_priority: int = 0
    no_data_dims: dict[str, list[str]] = field(default_factory=dict)

    def reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.excluded:
            out[e.field] = out.get(e.field, 0) + 1
        return out

    def no_data_histogram(self) -> dict[str, int]:
        hist: dict[str, int] = {}
        for dims in self.no_data_dims.values():
            for d in dims:
                hist[d] = hist.get(d, 0) + 1
        return dict(sorted(hist.items(), key=lambda kv: -kv[1]))


def apply_sieve(
    rows: Iterable[dict[str, Any]],
    exclude_words: Sequence[str],
) -> SieveOutcome:
    """按剔除词表过滤条目。rows 需含 priority / notes / req_id / title / section_path。"""
    words = {_norm(w) for w in exclude_words if _norm(w)}
    kept: list[dict[str, Any]] = []
    excluded: list[ExcludedItem] = []
    unclassified = 0
    no_data_dims: dict[str, list[str]] = {}

    for row in rows:
        priority = _norm(row.get("priority"))
        notes = _norm(row.get("notes"))
        hit_field, hit_word = "", ""
        if priority and priority in words:  # R1
            hit_field, hit_word = "priority", priority
        elif notes and notes in words:  # R2 (整格等值, 非子串)
            hit_field, hit_word = "notes", notes
        if hit_field:
            excluded.append(
                ExcludedItem(
                    req_id=str(row.get("req_id", "")),
                    title=str(row.get("title", "")),
                    section_path=str(row.get("section_path", "")),
                    priority=priority,
                    notes=notes,
                    field=hit_field,
                    matched_word=hit_word,
                    reason=f"{hit_field} 整格等于剔除词 {hit_word!r}",
                )
            )
            continue
        if priority in _EMPTY:  # R4
            unclassified += 1
        # R5: 短横线 = 该维度无数据, 不剔除, 仅记录 (等级已判过的字段不重复计)
        dims = [d for d in no_data_dimensions(row) if d not in {"priority"}]
        if dims:
            no_data_dims[str(row.get("req_id", ""))] = dims
        kept.append(row)

    return SieveOutcome(
        kept=kept,
        excluded=excluded,
        unclassified_priority=unclassified,
        no_data_dims=no_data_dims,
    )
