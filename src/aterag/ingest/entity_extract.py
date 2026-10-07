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

from aterag.extract.models import TableSchemaUnmapped
from aterag.ingest.markdown_parser import Block
from aterag.ingest.table_schema import (
    DEFAULT_SCHEMA_PATH,
    Detection,
    SchemaRegistry,
    TableSchema,
    is_rail_name,
    load_registry,
)

_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
_RANGE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*[~～]\s*([+-]?\d+(?:\.\d+)?)$")

# 需要转成 float 的规范字段; 其余字段按原样字符串携带
_NUMERIC_FIELDS = frozenset({"min", "typ", "max", "current_rating"})
# Requirement 实体的稳定字段契约: 缺失也要补 None/"", 下游可无脑取值
_REQUIREMENT_DEFAULTS = ("rail", "channel_no", "unit", "priority", "notes", "exists")
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
    # 红线 12 (2026-10-07 用户裁定): 无连接器的信号行**必须产实体**, 不得静默丢。
    #
    # 实测 4.2.4.3 第二张表 (信号名称|属性|电平|源/宿|说明, 11 行) 是板级总线
    # 信号的属性/电平/说明, 无连接器维度 —— 旧条件 `not connector or not (pin or
    # sig_name)` 把它连同内容一起扔了多年。现条件只丢「既无管脚也无信号名」的
    # 行 (纯表头分隔行, 无可抽内容)。无管脚行的 eid 尾段为空 (`模型:信号名@`),
    # 与带管脚实体 (`模型:信号名@S4`) 天然可区分, 不冲突。
    if not (pin or sig_name):
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


def _unmapped_table_message(reg: SchemaRegistry, det: Detection, section_path: str) -> str:
    """表头未命中任何 schema 时的报错正文(红线 12: 必须可操作)。

    要回答三个问题, 缺一个这条报错就只是「出错了」:

    1. **是哪张表** —— 章节路径 + 表头签名(签名可直接用于聚合同一类失败)
    2. **现在有哪些表结构可选** —— 逐个列出全部 schema 的表头, 人才能对着抄
    3. **系统猜它是什么** —— T1 兜底角色推断(:attr:`Detection.roles`),
       这是选新 schema 时最省事的起点
    """
    lines = [
        "表头未命中任何表结构 schema, 而这张表有数据行 —— 规格书表头已变"
        "(改名 / 加列 / 换列名)。",
        "",
        f"  位置: 章节 {section_path or '(无章节号)'}",
        f"  表头签名: {det.signature}",
        f"  判定: {det.reason}",
        "",
        f"  现有表结构 ({len(reg.schemas)} 个, 改 config/table_schemas.yaml 的 schemas):",
    ]
    for schema in reg.schemas:
        cols = ", ".join(schema.columns) if schema.columns else "(无列映射)"
        lines.append(f"    {schema.name:<20} [{schema.entity}] {cols}")
    lines += [
        "",
        "  处置:",
        "    1. 这是新表 -> 在 config/table_schemas.yaml 的 schemas 下加一条"
        "(columns 的值必须在 fields 白名单内);",
        "    2. 已有表改了表头 -> 改对应 schema 的 columns 键以匹配新表头;",
        "    3. 这张表本就不该抽实体 -> 给它加一条 entity: none 的 schema 并声明"
        "require, 别靠「没命中就算元数据表」—— 那正是本次修掉的静默。",
        "    生成候选: python scripts/table_schema_report.py --propose",
    ]
    return "\n".join(lines)


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
    # 输出通道编号: 按**表格内出现顺序**编号, 与轨名无关 —— 换产品若 12V 排在
    # -54V 之前, 它就是输出1通道。作用域限于单张表: 各表覆盖的指标不同,
    # 跨表累计会让编号随章节顺序漂移。
    channel_counter: dict[tuple[int, str, str], dict[str, int]] = {}
    table_seq = 0

    for b in blocks:
        for table in b.tables:
            if not table:
                continue
            det = reg.detect(table[0], table[1:])
            # 通道编号只在**复合列且子列确为轨名**时才有意义: 表头重复列名才
            # 声明了"条目名由子列组成", 而那一子列还须是轨名写法。
            # 只看重复列名不够 —— 安规表22 表头也有重复的「等级」列, 其中间列是
            # 绝缘试验电压(4000Vdc), 会凭空多出 CH1/CH2/CH3 三路不存在的输出通道。
            header = [_clean(c) for c in table[0]]
            has_subcol = bool(header) and len(header) != len(set(header))
            if not det.produces_entities:
                # **两种「不产实体」必须分开**(红线 12):
                #
                # - ``det.matched`` 为真 = schema 命中但声明 entity=none ->
                #   元数据表(修改记录/标准清单), 跳过是对的。
                # - ``det.matched`` 为假 = 表头一个 schema 都没命中。实测把
                #   「编号」改成「条目号」后 5 个实体只剩 1 个 Product, 而当时
                #   这里是无条件 continue —— 零报错零警告, 产出一份看起来正常的
                #   错误结果。那是抽取过程的第三种结局, 红线 12 明确排除。
                if det.matched:
                    continue
                # 空表不报: 没有数据行就没有可丢的知识, 报它是噪音。
                if len(table) <= 1:
                    continue
                raise TableSchemaUnmapped(
                    _unmapped_table_message(reg, det, b.section_path),
                    signature=det.signature,
                    section_path=b.section_path,
                )
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
            # 键用 (表序号, 章节, 表头) 而非 id(table): table 是 list, 临时对象回收后
            # id() 会被下一个对象复用, 两张不同的表就会共享通道计数器 ——
            # 结果是第二张表从上一张表的通道数继续编, CH1 凭空消失。
            table_seq += 1
            tkey = (table_seq, b.section_path, "|".join(header))
            table_channels = channel_counter.setdefault(tkey, {})
            for cells in table[1:]:
                row = reg.map_row(det.schema, table[0], cells)
                if not row:
                    continue
                key = (b.section_path, row.get("req_id", ""))
                seq = seq_counter.get(key, 0)
                seq_counter[key] = seq + 1
                # 通道编号按该轨在本表内首次出现的次序给定。无轨行(整机级要求,
                # 如 SR-1204 输出功率 / SR-1210 整机效率)不占通道号 —— 它适用于
                # 全部输出轨, 编号它会让"第N通道"这个概念凭空多出不存在的一路。
                rail = _clean(row.get("rail", ""))
                if has_subcol and not is_rail_name(rail):
                    # 子列不是轨名写法(如绝缘试验电压 4000Vdc): 该列不是输出通道,
                    # 不能据此编号 —— 否则会凭空造出不存在的输出路数。
                    rail = ""
                if has_subcol and rail and rail not in table_channels:
                    table_channels[rail] = len(table_channels) + 1
                if has_subcol and rail:
                    row["channel_no"] = str(table_channels[rail])
                else:
                    row.pop("channel_no", None)
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
    # group 值装 {"direction": ..., "parts": {...}}: direction 必须**跟着 group 走**。
    # 之前它是循环里的局部变量, 到第二个循环只剩最后一次迭代的值。
    groups: dict[tuple[str, str], dict[str, Any]] = {}
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
        groups.setdefault((base_name, rail), {"direction": direction, "parts": {}})["parts"][kind] = e

    for (base_name, rail), group in groups.items():
        parts = group["parts"]
        src = parts.get("保护点") or parts.get("保护")
        if not src:
            continue
        props: dict[str, Any] = {
            "protection_type": base_name,
            "trip_min": src.props.get("min"),
            "trip_max": src.props.get("max"),
            "req_id": src.props.get("req_id"),
            "direction": group["direction"],
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
