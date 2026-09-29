"""实体抽取: 表结构档案驱动 (声明式表头映射 + 通用列角色推断).

PA601 类规格书的参数表结构高度规整, 规则解析即可获得确定性的实体与参数值;
但"表头词 -> 规范字段"这份认知原先硬编码在本模块 (_HEADER_MAP / _detect_header /
_align_row / 三分支分发), 实测全文档 22 个表头签名有 12 个失配 —— 遥测表 18 行整表
丢失、遥信表"信号要求"列内容被静默丢弃。现改为三层外置:

  * 表头语义  -> config/table_schemas.yaml (SchemaRegistry, 换模板 = 改配置)
  * 行布局    -> table_schema.ROW_STRATEGIES (按名注册)
  * 实体构造  -> 本模块 ENTITY_BUILDERS (按名注册)

本模块只保留编排、数值解析与派生分组逻辑, 不含任何具体文档的表头词。
LLM 仅用于产品类型分类与语义消歧 (见 ingest.classify), 不参与表结构识别。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aterag.ingest.markdown_parser import Block
from aterag.ingest.table_schema import (
    DEFAULT_SCHEMA_PATH,
    SchemaRegistry,
    TableSchema,
    load_registry,
)

_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
_RANGE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*[~～]\s*([+-]?\d+(?:\.\d+)?)$")

# 需要转成 float 的规范字段; 其余字段按原样字符串携带
_NUMERIC_FIELDS = frozenset({"min", "typ", "max", "current_rating"})
# Requirement 实体的稳定字段契约: 缺失也要补 None/"", 下游可无脑取值
_REQUIREMENT_DEFAULTS = ("rail", "unit", "priority", "notes", "exists")
_REQUIREMENT_NUM_DEFAULTS = ("min", "typ", "max")

# 同一需求编号常有多行 (多档位/多电压轨/长期短期/多试验点), 只用 req_id+rail
# 作 eid 会把它们合并 —— 实测 PA601 有 11 个编号 23 行被静默丢弃 (SR-1210 三档效率
# 只剩一档)。故追加一个由"区分性字段"构成的短后缀: 值确定、可溯源, 且唯一。
# 顺序经实测校准 (PA601 全文档零碰撞): 电压轨 -> 单位 -> 限值 -> 备注/要求/标准。
# notes 排在限值之后: 备注是长句, 进 eid 可读性差且易超长。
_VARIANT_FIELDS = ("rail", "unit", "min", "typ", "max", "notes", "requirement_text", "standard")
_VARIANT_MAX = 24
_PLACEHOLDERS = {"", "-", "—", "/"}


def _variant_base(row: dict[str, str]) -> str:
    """先取无需行号的稳定区分字段 (限值最易读, 其次单位/电压轨)。"""
    for f in _VARIANT_FIELDS:
        v = _clean(row.get(f, ""))
        if v not in _PLACEHOLDERS:
            return v if f in ("rail", "unit") else f"{f}={v}"
    return "base"


def _variant_suffix(row: dict[str, str], *, unique: str = "") -> str:
    """档位后缀。unique 为该编号下的出现序号 (0 起), 仅在基础后缀不足以区分时追加。"""
    base = _variant_base(row)
    if not unique:
        return base
    try:
        idx = int(unique)
    except ValueError:
        return base
    if idx == 0 and base != "base":
        return base
    tag = f"#{idx + 1}"
    return base if len(base) + len(tag) > _VARIANT_MAX else base + tag


@dataclass
class Entity:
    etype: str  # Requirement | Protection | Parameter | Interface | Signal | Attribute | Product
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


# ---------------- 实体构造器 (按 schema.entity 分发) ----------------


@dataclass
class BuildContext:
    row: dict[str, str]
    schema: TableSchema
    block: Block
    base: dict[str, Any]  # section_path / heading / model_id / parent_headings
    seq: int = 0  # 同一 (章节, 需求编号) 下的出现序号, 用于区分多档位行


def _generic_props(ctx: BuildContext) -> dict[str, Any]:
    """行里映射到的规范字段全量携带。

    旧实现用 props 白名单裁剪, 导致"信号名称/信号要求"列即使被解析出来也在
    构造实体时被丢弃 (遥信表 50 行实测量 signal_req 命中数为 0)。改为全量携带
    后, 新模板的列不会再需要改代码。
    """
    props: dict[str, Any] = dict(ctx.base)
    for f, v in ctx.row.items():
        props[f] = _parse_num(v) if f in _NUMERIC_FIELDS else v
    return props


def _build_requirement(ctx: BuildContext, add: Callable[[Entity], None], reg: SchemaRegistry):
    row = ctx.row
    req_id = row.get("req_id", "")
    # 遥测表没有"项目"列, 以"遥测量"作为条目名 (telemetry_table 映射到 subject)
    title = row.get("title", "") or row.get("subject", "")
    if not req_id or not title:
        return
    props = _generic_props(ctx)
    props["req_id"] = req_id
    props["title"] = title
    for f in _REQUIREMENT_DEFAULTS:
        props.setdefault(f, row.get(f, ""))
    for f in _REQUIREMENT_NUM_DEFAULTS:
        props.setdefault(f, _parse_num(row.get(f, "")))
    rule = reg.grouping_protection
    if rule and rule.matches_section(ctx.base.get("section_path", "")):
        props["category"] = "protection"
    add(Entity("Requirement", f"{req_id}@{_variant_suffix(row, unique=str(ctx.seq))}", props))


def _build_signal(ctx: BuildContext, add: Callable[[Entity], None], reg: SchemaRegistry):
    row = ctx.row
    sig_name = row.get("signal_name", "")
    sig_def = row.get("signal_def", "")
    pin = row.get("pin", "")
    if not row.get("connector", "") or not (pin or sig_name):
        return
    model_id = ctx.base.get("model_id", "")
    add(
        Entity(
            "Signal",
            f"{model_id}:{sig_def or sig_name}@{pin}",
            _generic_props(ctx),
        )
    )


def _build_attribute(ctx: BuildContext, add: Callable[[Entity], None], reg: SchemaRegistry):
    """键值型属性表 (如通信协议"基本参数|描述")。"""
    row = ctx.row
    key = row.get("key", "")
    if not key:
        return
    model_id = ctx.base.get("model_id", "")
    section = ctx.base.get("section_path", "")
    add(Entity("Attribute", f"{model_id}:{section}:{key}", _generic_props(ctx)))


def _build_none(ctx: BuildContext, add: Callable[[Entity], None], reg: SchemaRegistry):
    """元数据表 (修改记录/标准清单) —— 明确不产生实体。"""


ENTITY_BUILDERS: dict[str, Callable[..., None]] = {
    "requirement": _build_requirement,
    "signal": _build_signal,
    "attribute": _build_attribute,
    "none": _build_none,
}


def extract_from_blocks(
    blocks: list[Block],
    model_id: str,
    doc_version: str = "",
    registry: SchemaRegistry | None = None,
) -> list[Entity]:
    """从解析块中抽取本体实体 (确定性规则, 表结构语义来自档案)。"""
    reg = registry or load_registry(DEFAULT_SCHEMA_PATH)
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

    # 同一 (章节, 需求编号) 的出现序号: 多档位行需要它来生成唯一 eid
    seq_counter: dict[tuple[str, str], int] = {}

    for b in blocks:
        for table in b.tables:
            if not table:
                continue
            det = reg.detect(table[0], table[1:])
            if not det.produces_entities:
                # 未映射 / 元数据表: 行仍保留在 blocks.jsonl (无损底座),
                # 缺口由 scripts/table_schema_report.py 显式列出
                continue
            builder = ENTITY_BUILDERS.get(det.schema.entity)  # type: ignore[union-attr]
            if builder is None:
                raise ValueError(
                    f"schema {det.schema.name} 声明了未注册的实体构造器 {det.schema.entity!r}"
                )
            base = {
                "section_path": b.section_path,
                "heading": b.heading,
                "model_id": model_id,
                "parent_headings": b.parent_headings,
            }
            for cells in table[1:]:
                row = reg.map_row(det.schema, table[0], cells)
                if not row:
                    continue
                key = (b.section_path, row.get("req_id", ""))
                seq = seq_counter.get(key, 0)
                seq_counter[key] = seq + 1
                builder(
                    BuildContext(row=row, schema=det.schema, block=b, base=base, seq=seq),
                    add,
                    reg,
                )

    # 保护分组: 点/恢复点/回差 聚合为 Protection 实体 (章节与标题模式来自档案)
    _group_protections(entities, reg)
    return entities


def _group_protections(entities: list[Entity], reg: SchemaRegistry) -> None:
    """把 '输入过压保护点/恢复点/回差' 行聚合为 Protection 实体。"""
    rule = reg.grouping_protection
    if rule is None:
        return
    reqs = [e for e in entities if e.etype == "Requirement"]
    groups: dict[tuple[str, str], dict[str, Entity]] = {}
    for e in reqs:
        m = rule.title_pattern.match(e.props.get("title", ""))
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
