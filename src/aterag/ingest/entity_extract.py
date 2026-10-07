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
from collections.abc import Callable, Sequence
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
# 只剩一档)。故追加一个"档位后缀"保证唯一。
#
# ## 后缀只能由**语义**构成, 不能由判据数值构成
#
# 旧实现按 ("rail","unit","min","typ","max","notes","requirement_text") 取第一个
# 非空值, 于是限值成了键的一部分: ``SR-PA601-D54A-1104@min=0.95#2``。后果是
# **改判据就换 id** —— 判据无法版本化, 且回答不了「历史测试记录依据的是哪一版」,
# 那正是红线 5 (出处必须可查) 要的东西。实测 PA601 上这类 id 有 57 个。
#
# 改为按语义顺序取: 电压轨 -> 工况标签(由既有 condition_patterns 规则抽出)
# -> 引用标准 -> 单位, 都取不到才回落到出现序号 ``#N``。判据数值一律不进键。
#
# ## 工况标签复用既有规则库, 不在本模块重写正则
#
# 标签值来自 :func:`_semantic_tags`, 它读的是 ``config/condition_patterns.yaml``
# 那 37 条规则 —— 与 :mod:`aterag.extract.assembler` 切条件用的是**同一份**口径。
# 若在这里另写一套正则, 同一句话就会有两个抽取结果 (红线 4: 不接受双源)。
_VARIANT_MAX_TAGS = 3
_VARIANT_MAX = 96
#: 单个标签值的长度上限。``load_step`` 那类没声明 group 的规则只能退回命中片段,
#: 片段可能很长("...负载变化") —— 限长是为了 eid 可读, 不是防注入。
_VARIANT_TAG_VALUE_MAX = 24
_PLACEHOLDERS = {"", "-", "—", "/"}

#: 参与 eid 档位标签的规则由配置声明 (``variant: true``), 这里不枚举 kind。
#:
#: **不能用 kind**: ``load`` 下同时挂着「负载百分比」「半载」「空载」「负载范围」
#: 「阶跃」「突变速率」六条规则 —— 实测按 kind 打标会把突变速率 0.1A/uS 标成
#: ``load=0.1``(看着像"负载 0.1%"), 把范围下界 50 标成 ``load=50``(看着像
#: "50% 负载点")。两者都是**物理量不同的东西共用一个名字**, 而 eid 是要被
#: 人读出来判断"这行要在什么工况测"的。规则 id 才是语义身份。
#:
#: 工况抽取规则库路径 (与 extract/assembler 的默认值同源, 避免两处各写一遍)。
_DEFAULT_PATTERNS_PATH = "config/condition_patterns.yaml"

_BOOK_CACHE: dict[str, Any] = {}


def _pattern_book(model_id: str):
    """取该型号的条件规则书 (进程内缓存; 载入失败返回 None 而非抛错)。

    载入失败必须降级而不是中断抽取: 工况标签只是**锦上添花的区分度**, 缺了它
    eid 会退到 ``#N``, 仍然唯一 (见 :func:`_variant_suffix`)。若在这里抛错,
    一份配置写错就会让整个型号抽不出实体 —— 那是把可选增强变成硬依赖。
    """
    if model_id in _BOOK_CACHE:
        return _BOOK_CACHE[model_id]
    book = None
    try:
        from aterag.extract.assembler import PatternBook

        book = PatternBook.load(_DEFAULT_PATTERNS_PATH)
    except Exception:  # noqa: BLE001 - 配置缺失/损坏都不应阻断抽取
        book = None
    _BOOK_CACHE[model_id] = book
    return book


def _semantic_tags(row: dict[str, str], book: Any) -> list[str]:
    """抽工况短标签, 如 ``load_pct_of_max=20`` / ``load_slew_rate=0.1``。

    只用配置里声明了 ``variant: true`` 的规则。标签名取**规则 id**而不是 kind
    (理由见 :data:`_VARIANT_TAG_KINDS` 上方注释)。

    标签值按 ``capture`` 声明的语义取, 与 :func:`assembler._capture_value` 一致:
    有 ``group`` 取该捕获组; 另声明了 ``group2`` 则两段都用 ``-`` 连接
    (``load_range`` 的 50~100%); 有定值 ``value`` 用定值(``load_half`` -> 50);
    都没有时取**命中片段本身**(已去空白, 限长)—— ``load_step`` 没声明 group,
    但「25%~50%~25% 负载变化」这个片段本身就是工况, 比只留规则名有区分度。
    """
    if book is None:
        return []
    text = " ".join(
        v for v in (_clean(row.get("notes", "")), _clean(row.get("requirement_text", ""))) if v
    )
    if not text:
        return []
    tags: set[str] = set()
    for rule in book.rules:
        if not getattr(rule, "variant", False):
            continue
        m = rule.regex.search(text)
        if not m:
            continue
        cap = rule.capture or {}
        parts: list[str] = []
        for key in ("group", "group2"):
            gi = cap.get(key)
            if isinstance(gi, int) and 0 < gi <= (m.re.groups or 0):
                parts.append((m.group(gi) or "").strip())
        if not parts and cap.get("value") is not None:
            parts.append(str(cap["value"]))
        if not parts:
            parts.append(_norm_tag(m.group(0)))
        val = "-".join(p for p in parts if p)
        tags.add(f"{rule.id}={val}" if val else rule.id)
    # 排序保证同一组标签在任何机型上顺序一致 —— 否则 id 不可复现。
    return sorted(tags)


def _variant_suffix(
    row: dict[str, str], *, unique: str = "", tags: Sequence[str] = ()
) -> str:
    """档位后缀: 电压轨 + 工况标签 + 标准 + 单位, 全缺才用出现序号 ``#N``。

    ``#N`` 仍保留: 有些表格(如 §4.4.2 的 12 行 DIP)每行是一个**枚举出来的独立
    工况**, 语义标签与标准都相同, 只有行序能区分 —— 那时行序就是它唯一的身份。
    """
    parts: list[str] = []
    rail = _clean(row.get("rail", ""))
    if rail and rail not in _PLACEHOLDERS:
        parts.append(f"rail={rail}")
    parts.extend(tags[:_VARIANT_MAX_TAGS])
    if len(parts) < _VARIANT_MAX_TAGS:
        std = _clean(row.get("standard", ""))
        if std and std not in _PLACEHOLDERS:
            parts.append(f"std={std}")
        if len(parts) < _VARIANT_MAX_TAGS:
            unit = _clean(row.get("unit", ""))
            if unit and unit not in _PLACEHOLDERS:
                parts.append(f"unit={unit}")
    base = "+".join(parts) if parts else "base"
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


# ---------------------------------------------------------------------------
# 主轨归属: 未标注轨的参数行按主轨计
# ---------------------------------------------------------------------------
#: 主轨**不是**能从文档自动推出来的东西, 而是型号事实: PA601-D54A 主轨 -54V
#: (11.1A), PN1000-48A 主轨 -48V (20.8A) —— 同模板不同型号主轨不同, 所以它必须
#: 按型号声明 (``data/registry.yaml`` 的 products.<model>.main_rail``), 声明了才生效。
#:
#: **不自动推断**: 若按「带轨行里出现最多者」或「额定电流最大者」去猜, 猜错时
#: 会把效率、功率、待机功耗这些判据挂到一条不存在的输出路上, 而且不报错。
#: 宁可缺声明而不猜 —— 缺声明时下面三条规则全部不触发, 实体保持 rail=''。
_MAIN_RAIL_SKIP_UNITS = re.compile(r"[VA]ac|Vdc", re.IGNORECASE)
_MAIN_RAIL_SKIP_NOTES = re.compile(r"[+-]?\d+(?:\.\d+)?\s*V(?![acdAC])")


def main_rail_declared(model_id: str) -> str:
    """读产品注册表里该型号声明的主轨; 未声明返回 ``""``。

    **走 :class:`aterag.registry.Registry` 而不是自己 yaml.safe_load** —— 注册表
    定位有两级兜底(``settings.registry_path`` -> ``data/<basename>``), 自己读
    相对路径 ``data/registry.yaml`` 只在 CWD 恰好是仓库根时成立, 而抽取是在
    各种 CWD 下被调的(CLI / MCP server / 测试)。更实际的问题: 注册表**条目类型**
    由 ``storage.model_schema`` 定义过, 绕过它读 yaml 就等于承认第二份
    ``main_rail`` 可以存在(红线 4)。

    读不到时返回空串而不是抛错: 主轨是**增强**, 缺声明时行保持无轨(不猜),
    不该让一份配置缺失阻断整个型号的实体抽取。
    """
    try:
        from aterag.config import get_settings
        from aterag.registry import Registry

        prod = Registry.load(get_settings()).products.get(model_id)
        return _clean(str(getattr(prod, "main_rail", "") or ""))
    except Exception:  # noqa: BLE001 - 定位失败/格式不符 => 视为未声明, 不猜
        return ""


def resolve_main_rail(row: dict[str, str], main_rail: str) -> str:
    """未标注轨的参数行 -> 主轨; 有下列可判例外时**保持无轨**。

    例外都必须是**从数据本身判得出**的, 不能列 id 白名单 —— 同模板的其它型号
    (PN1000-48A / PN2000-24A)行数与 id 都不同, 白名单换个型号就失效:

    1. **输入侧量**: 单位含 ``Vac``/``Vdc``。判据说的是输入电压/频率, 挂输出轨
       没有意义 —— SR-1300 输入过压保护点、SR-1104 功率因数属此类。
    2. **温度量**: 单位是 ``℃``。过温保护点/回差没有"哪条轨"的概念。
    3. **跨轨项**: 备注点名了**两条以上**电压轨。SR-1222「-54V、3.45V输出要求
       ORING」、SR-1225「3.45V和-54V不共地」、SR-1312「3.45V保护不能影响-54V的
       输出」都是整机级关系, 归到任一单轨都是错的。
    4. **无单位的整机级行**: 单位是占位符。SR-1223 热插拔、SR-1224 上下电时序
       说的是整机行为, 不是某条轨上的电气量。
    """
    if not main_rail:
        return ""
    unit = _clean(row.get("unit", ""))
    if unit in _PLACEHOLDERS:
        return ""  # 例外 4
    if _MAIN_RAIL_SKIP_UNITS.search(unit):
        return ""  # 例外 1
    if "℃" in unit or "°C" in unit:
        return ""  # 例外 2
    notes = _clean(row.get("notes", ""))
    if notes and len(set(_MAIN_RAIL_SKIP_NOTES.findall(notes))) >= 2:
        return ""  # 例外 3
    return main_rail


@dataclass
class Entity:
    etype: str  # Requirement | Protection | Parameter | Interface | Signal | Attribute | Product
    eid: str
    props: dict[str, Any] = field(default_factory=dict)


def _clean(v: str) -> str:
    return (v or "").strip().replace("<br>", " ").replace("**", "")


#: 标签值里的空白与分隔符归一 —— 同一条规则在两台机器/两个版本 yaml 上编出来
#: 必须给同一个标签, 否则 eid 不可复现 (id 是主键的一部分)。
def _norm_tag(v: str) -> str:
    v = re.sub(r"\s+", "", v or "")
    return v[:_VARIANT_TAG_VALUE_MAX]


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
    add(
        Entity(
            "Requirement",
            f"{req_id}@{_variant_suffix(row, unique=str(ctx.seq), tags=_semantic_tags(row, _pattern_book(ctx.base.get('model_id', ''))))}",
            props,
        )
    )


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
            # 主轨按**型号**声明 (data/registry.yaml 的 products.<model>.main_rail)。
            # 同模板不同型号主轨不同 (PA601-D54A=-54V/11.1A, PN1000-48A=-48V/20.8A),
            # 所以不能配在 profile 层。未声明时下面一律不挂主轨 —— 不猜。
            main_rail = main_rail_declared(model_id)
            for cells in table[1:]:
                row = reg.map_row(det.schema, table[0], cells)
                if not row:
                    continue
                key = (b.section_path, row.get("req_id", ""))
                seq = seq_counter.get(key, 0)
                seq_counter[key] = seq + 1
                # 通道编号按该轨在本表内首次出现的次序给定。
                rail = _clean(row.get("rail", ""))
                if has_subcol and not is_rail_name(rail):
                    # 子列不是轨名写法(如绝缘试验电压 4000Vdc): 该列不是输出通道,
                    # 不能据此编号 —— 否则会凭空造出不存在的输出路数。
                    rail = ""
                if has_subcol and not rail:
                    # 未标注轨的参数行 -> 主轨(用户裁定)。例外条件见
                    # :func:`resolve_main_rail` 的四条, 全部从数据本身判, 不列 id。
                    # 判据仍然只是**单轨**归属性: 跨轨项(备注点名两条轨)保持无轨。
                    rail = resolve_main_rail(row, main_rail)
                    if rail:
                        # 必须**写回 row**: 下游的 props / eid / 通道号都从 row 取值,
                        # 只改局部变量的话主轨在这一行就丢了 —— 实测过: 通道号拿到了 1,
                        # 而 props["rail"] 仍是空, 于是 eid 里没有 rail 段, 且
                        # "这条判据属于哪条轨"查不到。
                        row["rail"] = rail
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
