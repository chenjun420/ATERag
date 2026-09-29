"""实体抽取: 规格书表格 -> 本体实体 (规则驱动, LLM 兜底).

PA601 类规格书的参数表结构高度规整 (编号/项目/单位/最小/典型/最大/备注/等级),
规则解析即可获得确定性的实体与参数值; LLM 仅用于产品类型分类与语义消歧。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aterag.ingest.markdown_parser import Block

_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
_RANGE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*[~～]\s*([+-]?\d+(?:\.\d+)?)$")

_RAIL_RE = re.compile(r"^-?\d+(?:\.\d+)?V(?:dc)?$", re.IGNORECASE)

# 表头识别关键字 -> 规范字段
_HEADER_MAP = {
    "编号": "req_id",
    "项目": "title",
    "单位": "unit",
    "最小值": "min",
    "典型值": "typ",
    "最大值": "max",
    "备注": "notes",
    "等级": "priority",
    "信号名称": "signal_name",
    "信号要求": "signal_req",
    "检测范围": "range_text",
    "精度": "accuracy",
    "管脚": "pin",
    "信号定义": "signal_def",
    "通流量": "current_rating",
    "通流能力": "current_rating",
    "连接器": "connector",
    "属性": "attr",
    "电平": "level",
    "源/宿": "src_dst",
    "说明": "notes",
}


@dataclass
class Entity:
    etype: str  # Requirement | Protection | Parameter | Interface | Signal | Product
    eid: str
    props: dict[str, Any] = field(default_factory=dict)


def _clean(v: str) -> str:
    return (v or "").strip().replace("<br>", " ").replace("**", "")


def _parse_num(v: str) -> float | None:
    """剥离单位后取首个数值: '1A'->1, '±3%'->None(非纯数值), '-'->None。

    必须同时接受显式正号: 规格书常把正轨电压写成 '+3.45' (如 SR-PA601-D54A-1200#3.45V),
    原正则只有 `-?`, 导致该值整条丢失且不报错 —— 属于静默数据丢失。
    """
    v = _clean(v)
    if not v or v in {"-", "—", "无"}:
        return None
    m = re.match(r"([+-]?\d+(?:\.\d+)?)\s*[A-Za-z%Ωμ]*$", v)
    if m:
        return float(m.group(1))
    return None


def parse_range(v: str) -> tuple[float | None, float | None]:
    """'12~18' -> (12, 18); 单值放 min。"""
    v = _clean(v)
    m = _RANGE.match(v)
    if m:
        return float(m.group(1)), float(m.group(2))
    n = _parse_num(v)
    return (n, None) if n is not None else (None, None)


def _detect_header(cells: list[str]) -> dict[int, str] | None:
    """表头行 -> {列索引: 规范字段}; 至少命中 编号+项目 或 信号名称/连接器。"""
    mapped: dict[int, str] = {}
    for i, c in enumerate(cells):
        # 归一化: 去掉括号注记, 如 '通流能力(A)'/'通流量(A)' -> '通流能力'/'通流量'
        key = _HEADER_MAP.get(re.sub(r"[（(].*?[)）]", "", _clean(c)))
        if key:
            mapped[i] = key
    if ("req_id" in mapped.values() and "title" in mapped.values()) or (
        "connector" in mapped.values() and "signal_name" in mapped.values()
    ):
        return mapped
    return None


_UNIT_RE = re.compile(r"^[A-Za-z%/℃μΩ.·]+$")
_HASNO_RE = re.compile(r"^(有|无)$")


def _align_row(header: dict[int, str], n_header_cols: int, cells: list[str]) -> dict[str, str]:
    """锚点右对齐的行映射。

    参数表的实际行宽经常与表头不一致 (rail 子列/单元格错位)。
    可靠锚点: 编号=首列, 项目=次列, 等级=末列, 备注=次末列;
    数值列 (最小/典型/最大) 从右向左回填; 单位/rail/有无 从中间槽位识别。
    """
    row: dict[str, str] = {}
    if len(cells) == n_header_cols and not any(_RAIL_RE.match(_clean(c)) for c in cells[2:-1]):
        # 结构规整: 直接按表头映射
        for i, key in header.items():
            if i < len(cells):
                row[key] = _clean(cells[i])
        return row

    if len(cells) < 3:
        return row
    row["req_id"] = _clean(cells[0])
    row["title"] = _clean(cells[1])
    tail = list(cells)
    if "priority" in header.values():
        row["priority"] = _clean(tail[-1])
        tail = tail[:-1]
    if "notes" in header.values():
        row["notes"] = _clean(tail[-1])
        tail = tail[:-1]
    middle = tail[2:]

    # 从右向左回填数值列
    value_keys = [k for k in ("max", "typ", "min") if k in header.values()]
    vals: dict[str, str] = {}
    vi = 0
    for cell in reversed(middle):
        if vi >= len(value_keys):
            break
        if _RAIL_RE.match(_clean(cell)) or _HASNO_RE.match(_clean(cell)):
            continue  # 跳过 rail/有无, 不占用数值槽位
        vals[value_keys[vi]] = _clean(cell)
        vi += 1
    # 若数值槽位未填满且存在 '-' 之外的占位, 保持 None
    row.update({k: vals.get(k, "-") for k in ("min", "typ", "max")})

    # 中间槽位: rail / 单位 / 有无
    for cell in middle:
        c = _clean(cell)
        if _RAIL_RE.match(c) and not row.get("rail"):
            row["rail"] = c
        elif _UNIT_RE.match(c) and not row.get("unit"):
            row["unit"] = c
        elif _HASNO_RE.match(c) and not row.get("exists"):
            row["exists"] = c
    # 未被识别为单位的字母列兜底 (如 表12 的 A/V 在 rail 之后)
    if not row.get("unit"):
        for cell in middle:
            c = _clean(cell)
            if _UNIT_RE.match(c):
                row["unit"] = c
                break
    return row


_PROTECTION_TITLE_RE = re.compile(
    r"^(输入|输出)?(静态|动态)?(过压|欠压|过流|过温|短路)(?:保护)?(点|恢复点|回差)?$"
)


def extract_from_blocks(blocks: list[Block], model_id: str, doc_version: str = "") -> list[Entity]:
    """从解析块中抽取本体实体 (确定性规则)。"""
    entities: list[Entity] = []
    seen: set[tuple[str, str]] = set()

    def add(e: Entity) -> None:
        key = (e.etype, e.eid)
        if key not in seen:
            seen.add(key)
            entities.append(e)

    add(
        Entity(
            "Product",
            model_id,
            {"model_id": model_id, "doc_version": doc_version},
        )
    )

    for b in blocks:
        section = b.section_path
        heading = b.heading
        for table in b.tables:
            if not table:
                continue
            header = _detect_header(table[0])
            if not header:
                continue
            for cells in table[1:]:
                row = _align_row(header, len(table[0]), cells)
                if not row:
                    continue
                _row_to_entities(row, b, section, heading, model_id, add)

    # 保护分组: 点/恢复点/回差 聚合为 Protection 实体
    _group_protections(entities)
    return entities


def _row_to_entities(
    row: dict[str, str],
    block: Block,
    section: str,
    heading: str,
    model_id: str,
    add,
) -> None:
    req_id = row.get("req_id", "")
    title = row.get("title", "")
    base = {
        "section_path": section,
        "heading": heading,
        "model_id": model_id,
        "parent_headings": block.parent_headings,
    }

    # --- 接口定义表 (连接器/管脚/信号) ---
    if row.get("connector") and (row.get("pin") or row.get("signal_name")):
        sig_name = row.get("signal_name") or ""
        sig_def = row.get("signal_def") or ""
        add(
            Entity(
                "Signal",
                f"{model_id}:{sig_def or sig_name}@{row.get('pin')}",
                {
                    **base,
                    "connector": row.get("connector", ""),
                    "pin": row.get("pin", ""),
                    "signal_name": sig_name,
                    "signal_def": sig_def,
                    "current_rating": _parse_num(row.get("current_rating", "")),
                    "notes": row.get("notes", ""),
                },
            )
        )
        return

    if not req_id or not title:
        return

    # --- 遥测表 (检测范围/精度) ---
    if "range_text" in row or "accuracy" in row:
        add(
            Entity(
                "Requirement",
                req_id,
                {
                    **base,
                    "title": title,
                    "range_text": row.get("range_text", ""),
                    "accuracy": row.get("accuracy", ""),
                    "signal_name": row.get("signal_name", ""),
                    "priority": row.get("priority", ""),
                    "notes": row.get("notes", ""),
                },
            )
        )
        return

    # --- 参数类需求 (最小/典型/最大) ---
    mn, mx = _parse_num(row.get("min", "")), _parse_num(row.get("max", ""))
    rail = row.get("rail", "")
    entity = Entity(
        "Requirement",
        f"{req_id}{'#' + rail if rail else ''}",
        {
            **base,
            "req_id": req_id,
            "title": title,
            "rail": rail,
            "unit": row.get("unit", ""),
            "min": mn,
            "typ": _parse_num(row.get("typ", "")),
            "max": mx,
            "priority": row.get("priority", ""),
            "notes": row.get("notes", ""),
            "exists": row.get("exists", ""),
        },
    )
    # 保护功能章节的行同时标注 category
    if section.startswith("4.3.3"):
        entity.props["category"] = "protection"
    add(entity)


def _group_protections(entities: list[Entity]) -> None:
    """把 '输入过压保护点/恢复点/回差' 行聚合为 Protection 实体。"""
    reqs = [e for e in entities if e.etype == "Requirement"]
    groups: dict[str, dict[str, Entity]] = {}
    for e in reqs:
        m = _PROTECTION_TITLE_RE.match(e.props.get("title", ""))
        if not m:
            continue
        prefix, ptype, variant = m.group(1) or "", m.group(3), m.group(2) or ""
        base_name = f"{prefix}{ptype}{variant}保护"
        # 欠压类保护: 恢复点数值上高于保护点 (电压回升恢复), 方向与过压类相反
        direction = "under" if ptype == "欠压" else "over"
        suffix = m.group(4) or ""
        kind = {"点": "保护点", "恢复点": "恢复点", "回差": "保护回差", "": "保护"}.get(
            suffix, "保护"
        )
        rail = e.props.get("rail", "")
        groups.setdefault((base_name, rail), {})[kind] = e

    for (base_name, rail), parts in groups.items():
        src = parts.get("保护点") or parts.get("保护")
        if not src:
            continue
        props: dict[str, Any] = {
            "protection_type": base_name,
            "trip_min": src.props.get("min"),
            "trip_max": src.props.get("max"),
            "req_id": src.props.get("req_id"),
            "direction": direction,
            "rail": rail,
            "section_path": src.props.get("section_path", ""),
            "heading": src.props.get("heading", ""),
            "model_id": src.props.get("model_id", ""),
            "priority": src.props.get("priority", ""),
            "notes": src.props.get("notes", ""),
        }
        if "恢复点" in parts:
            props["recovery_min"] = parts["恢复点"].props.get("min")
            props["recovery_max"] = parts["恢复点"].props.get("max")
        if "保护回差" in parts:
            props["hysteresis_min"] = parts["保护回差"].props.get("min")
        entities.append(
            Entity(
                "Protection",
                f"{props.get('model_id', '')}:{base_name}{('#' + rail) if rail else ''}",
                props,
            )
        )
