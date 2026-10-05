"""表结构档案: 声明式表头映射 (T2) + 通用列角色推断 (T1).

设计约束 (对齐本仓库反幻觉基线):
  * 表结构认知全部外置 config/table_schemas.yaml, 本模块不含任何具体文档的中文词。
  * 运行时零 LLM: 语义映射只来自 YAML, 保证同一输入字节级可复现。
  * 未匹配表不产生实体 (避免污染实体库), 但行不丢: blocks.jsonl 是无损底座,
    且缺口一定会出现在 scripts/table_schema_report.py 的清单里。

三层自适应:
  T1 通用列角色推断 —— 零配置, 对任意表按值模式识别 id/numeric/unit/category/exists/text
  T2 声明式表头映射 —— config/table_schemas.yaml, 换模板 = 改配置
  T3 提案闭环       —— scripts/table_schema_report.py --propose 产出候选映射供人审,
                      审核后并入 T2; 运行时永不调 LLM
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SCHEMA_PATH = "config/table_schemas.yaml"

# ---------------- 表头单元格归一化 ----------------

_PAREN_RE = re.compile(r"[（(].*?[)）]")


def _clean_cell(v: str) -> str:
    return (v or "").strip().replace("<br>", " ").replace("**", "")


def _normalize_header(v: str) -> str:
    """表头归一化: 去括号注记与强调符。

    '通流量(A)' -> '通流量' (旧 _detect_header 的行为, 保留以兼容既有映射)。
    """
    return _PAREN_RE.sub("", _clean_cell(v))


# ---------------- T1 列角色推断 ----------------

_ROLE_ID = "id"
_ROLE_NUMERIC = "numeric"
_ROLE_UNIT = "unit"
_ROLE_CODE = "code"
_ROLE_EXISTS = "exists"
_ROLE_CATEGORY = "category"
_ROLE_TEXT = "text"
_ROLE_UNKNOWN = "unknown"

_ID_CELL_RE = re.compile(r"^(?:SR-)?[A-Z]{1,8}[-0-9A-Z]*\d+$")
_NUMERIC_CELL_RE = re.compile(
    r"^[+-]?\d+(?:\.\d+)?(?:\s*[~～]\s*[+-]?\d+(?:\.\d+)?)?\s*[A-Za-z%/℃μΩ]*$"
)
_UNIT_CELL_RE = re.compile(r"^[A-Za-z%/℃μΩ·]+$")
_BOOL_VALUES = frozenset({"有", "无", "是", "否"})

# 值模式占比阈值与最少样本量。样本太少时不做判定 (宁可 unknown 也不瞎猜)
_ROLE_MIN_RATIO = 0.6
_ROLE_MIN_SAMPLES = 3
_CATEGORY_MAX_DISTINCT = 4
_CATEGORY_MAX_LEN = 6


@dataclass(frozen=True)
class ColumnRole:
    """T1 推断出的列角色 —— 与具体文档词汇无关, 只看值形态。"""

    index: int
    header: str
    role: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "header": self.header,
            "role": self.role,
            "detail": self.detail,
        }


def infer_roles(header: Sequence[str], rows: Sequence[Sequence[str]] = ()) -> list[ColumnRole]:
    """按单元格值模式推断列角色, 不认识任何具体词汇。

    角色: id / numeric / unit / exists / category / text / unknown
    """
    n_cols = len(header)
    roles: list[ColumnRole] = []
    for i in range(n_cols):
        raw = _clean_cell(header[i]) if i < len(header) else ""
        cells = [_clean_cell(r[i]) for r in rows if i < len(r)]
        vals = [c for c in cells if c and c not in {"-", "—"}]
        if len(vals) < _ROLE_MIN_SAMPLES:
            roles.append(ColumnRole(i, raw, _ROLE_UNKNOWN, f"样本不足({len(vals)})"))
            continue
        n = len(vals)
        n_id = sum(1 for v in vals if _ID_CELL_RE.match(v))
        n_num = sum(1 for v in vals if _NUMERIC_CELL_RE.match(v))
        n_unit = sum(1 for v in vals if _UNIT_CELL_RE.match(v))
        n_bool = sum(1 for v in vals if v in _BOOL_VALUES)
        distinct = len(set(vals))

        if n_id / n >= _ROLE_MIN_RATIO:
            role, detail = _ROLE_ID, f"编号形态 {n_id}/{n}"
        elif n_num / n >= _ROLE_MIN_RATIO:
            role, detail = _ROLE_NUMERIC, f"数值形态 {n_num}/{n}"
        elif n_unit / n >= _ROLE_MIN_RATIO and distinct <= 6:
            unit_vals = {v for v in vals if _UNIT_CELL_RE.match(v)}
            # 单个 ASCII 字母既可能是单位(A 安培)也可能是等级码(性能判据 A/B/C)。
            # 不确定时如实标 code, 避免在接入报告里给出误导性的列映射建议。
            if unit_vals and all(len(v) == 1 and v.isascii() and v.isalpha() for v in unit_vals):
                role, detail = _ROLE_CODE, f"单字母取值({distinct}种), 单位/等级码歧义"
            else:
                role, detail = _ROLE_UNIT, f"单位形态 {n_unit}/{n}, 取值{distinct}种"
        elif n_bool / n >= 0.8:
            role, detail = _ROLE_EXISTS, f"有/无 {n_bool}/{n}"
        elif distinct <= _CATEGORY_MAX_DISTINCT and all(len(v) <= _CATEGORY_MAX_LEN for v in vals):
            role, detail = _ROLE_CATEGORY, f"低基数枚举 {distinct}种"
        else:
            role, detail = _ROLE_TEXT, f"自由文本, 取值{distinct}种"
        roles.append(ColumnRole(i, raw, role, detail))
    return roles


# ---------------- 行映射策略 (按名注册) ----------------

# 电压轨子列 (如 '-54V' / '3.45V') —— 判定行宽错位、需要走锚点回填
_RAIL_RE = re.compile(r"^-?\d+(?:\.\d+)?V(?:dc)?$", re.IGNORECASE)
_HASNO_RE = re.compile(r"^(有|无)$")
_UNIT_LIKE_RE = re.compile(r"^[A-Za-z%/℃μΩ.·]+$")
# 输出轨名: 必须是**不带千分位、不带 Vdc 后缀**的电压。绝缘试验电压
# (安规表22 的 "4000Vdc") 同样匹配 _RAIL_RE, 但它不是输出通道 —— 判据是量级与
# 写法: 输出轨写作 -54V / 3.45V / 12V, 而绝缘电压必带 Vdc 后缀且常是四位数。
_RAIL_NAME_RE = re.compile(r"^[+-]?\d{1,3}(?:\.\d+)?V$", re.IGNORECASE)


def is_rail_name(v: Any) -> bool:
    """该值是否是输出轨名 (而非任意电压量值)。"""
    return bool(_RAIL_NAME_RE.match(_clean_cell(str(v or ""))))


def _positional(mapped: Mapping[int, str], cells: Sequence[str]) -> dict[str, str]:
    """严格按表头索引映射, 不做任何启发式 (越界截断)。"""
    return {f: _clean_cell(cells[i]) for i, f in mapped.items() if i < len(cells)}


def _anchor_align(
    mapped: Mapping[int, str], n_header_cols: int, cells: Sequence[str]
) -> dict[str, str]:
    """锚点右对齐的行映射 (原 _align_row 行为, 原样保留)。

    需求表的行宽经常与表头不一致 (电压轨子列 / 单元格错位)。可靠锚点:
    编号=首列, 项目=次列, 等级=末列, 备注=次末列; 数值列从右向左回填;
    单位/电压轨/有无 从中间槽位识别。
    """
    if len(cells) == n_header_cols and not any(_RAIL_RE.match(_clean_cell(c)) for c in cells[2:-1]):
        return _positional(mapped, cells)

    row: dict[str, str] = {}
    if len(cells) < 3:
        return row
    fields = set(mapped.values())
    if "req_id" in fields:
        row["req_id"] = _clean_cell(cells[0])
    if "title" in fields:
        row["title"] = _clean_cell(cells[1])
    tail = list(cells)
    if "priority" in fields:
        row["priority"] = _clean_cell(tail[-1])
        tail = tail[:-1]
    if "notes" in fields:
        row["notes"] = _clean_cell(tail[-1])
        tail = tail[:-1]
    middle = tail[2:]

    value_keys = [k for k in ("max", "typ", "min") if k in fields]
    vals: dict[str, str] = {}
    vi = 0
    for cell in reversed(middle):
        if vi >= len(value_keys):
            break
        if _RAIL_RE.match(_clean_cell(cell)) or _HASNO_RE.match(_clean_cell(cell)):
            continue  # 电压轨/有无 不占数值槽位
        vals[value_keys[vi]] = _clean_cell(cell)
        vi += 1
    row.update({k: vals.get(k, "-") for k in ("min", "typ", "max") if k in fields})

    for cell in middle:
        c = _clean_cell(cell)
        if _RAIL_RE.match(c) and not row.get("rail"):
            row["rail"] = c
        elif _UNIT_LIKE_RE.match(c) and not row.get("unit"):
            row["unit"] = c
        elif _HASNO_RE.match(c) and not row.get("exists"):
            row["exists"] = c
    return row


ROW_STRATEGIES: dict[str, Callable[..., dict[str, str]]] = {
    "anchor_align": _anchor_align,
    "positional": lambda mapped, n_cols, cells: _positional(mapped, cells),
    # 原样保留原始行 (给未来"未映射表也要留痕"的路径用)
    "free": lambda mapped, n_cols, cells: {"raw": " | ".join(cells)},
}


# ---------------- Schema 定义 ----------------


@dataclass(frozen=True)
class TableSchema:
    name: str
    entity: str  # 实体构造器名: requirement | signal | attribute | none
    row_strategy: str  # ROW_STRATEGIES 的键
    columns: Mapping[str, str]  # 表头文本 -> 规范字段
    require: tuple[tuple[str, ...], ...] = ()  # 任一组全部命中即匹配
    meta: bool = False  # 已知忽略的元数据表
    reference: bool = False  # 有价值但非测试条件 (如空开选型参考)
    unmapped_headers: tuple[str, ...] = ()  # 声明了但未映射的表头 (加载期自检)

    def map_header(self, header: Sequence[str]) -> dict[int, str]:
        """表头行 -> {列索引: 规范字段}"""
        out: dict[int, str] = {}
        for i, raw in enumerate(header):
            field_name = self.columns.get(_normalize_header(raw))
            if field_name:
                out[i] = field_name
        return out


@dataclass
class Detection:
    """一次表头识别的完整结论 (含 T1 兜底), 供抽取与报告共用。"""

    header: list[str]
    signature: str
    schema: TableSchema | None
    mapped: dict[int, str]
    roles: list[ColumnRole]
    reason: str

    @property
    def matched(self) -> bool:
        return self.schema is not None

    @property
    def produces_entities(self) -> bool:
        return self.schema is not None and self.schema.entity != "none"

    def unmapped_headers(self) -> list[str]:
        mapped_cols = {i for i in self.mapped}
        return [_clean_cell(h) for i, h in enumerate(self.header) if i not in mapped_cols]


def signature_of(header: Sequence[str]) -> str:
    """表头签名: 用于报告聚合与稳定标识。"""
    return " | ".join(_clean_cell(h) for h in header)


# ---------------- 注册表 ----------------


@dataclass
class GroupingRule:
    sections: tuple[str, ...]
    title_pattern: re.Pattern[str]

    def matches_section(self, section_path: str) -> bool:
        return any(section_path.startswith(s) for s in self.sections)

    def matches_title(self, title: str) -> bool:
        return bool(self.title_pattern.match(title))


@dataclass
class SchemaRegistry:
    schemas: tuple[TableSchema, ...]
    grouping_protection: GroupingRule | None = None
    known_fields: frozenset[str] = frozenset()
    source_path: str = ""

    # ---- 加载 ----
    @classmethod
    def load(cls, path: str | Path = DEFAULT_SCHEMA_PATH) -> SchemaRegistry:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"表结构档案不存在: {p} (表结构认知的唯一来源, 缺失会静默丢列, 故直接报错)"
            )
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        known = frozenset(doc.get("fields") or ())
        schemas: list[TableSchema] = []
        for name, spec in (doc.get("schemas") or {}).items():
            schemas.append(cls._build_schema(name, spec, known))
        g = (doc.get("grouping") or {}).get("protection")
        grouping = None
        if g:
            grouping = GroupingRule(
                sections=tuple(g.get("sections") or ()),
                title_pattern=re.compile(g["title_pattern"]),
            )
        return cls(
            schemas=tuple(schemas),
            grouping_protection=grouping,
            known_fields=known,
            source_path=str(p),
        )

    @classmethod
    def _build_schema(
        cls, name: str, spec: Mapping[str, Any], known: frozenset[str]
    ) -> TableSchema:
        columns = dict(spec.get("columns") or {})
        bad = {f for f in columns.values() if f not in known}
        if bad:
            raise ValueError(
                f"schema {name}: columns 映射到白名单外的规范字段 {sorted(bad)} "
                f"(fields: {sorted(known)})"
            )
        match = spec.get("match") or {}
        require: list[tuple[str, ...]] = []
        if match.get("require"):
            require.append(tuple(match["require"]))
        for group in match.get("any_of") or ():
            require.append(tuple(group.get("require") or ()))
        for group in require:
            missing = [f for f in group if f not in known]
            if missing:
                raise ValueError(f"schema {name}: match.require 引用未知字段 {missing}")
        strategy = spec.get("row_strategy") or "positional"
        if strategy not in ROW_STRATEGIES:
            raise ValueError(f"schema {name}: 未注册的 row_strategy {strategy!r}")
        declared = {_normalize_header(h) for h in columns}
        return TableSchema(
            name=name,
            entity=spec.get("entity") or "requirement",
            row_strategy=strategy,
            columns=columns,
            require=tuple(require),
            meta=bool(spec.get("meta", False)),
            reference=bool(spec.get("reference", False)),
            unmapped_headers=tuple(sorted(declared - {_normalize_header(h) for h in columns})),
        )

    def by_name(self, name: str) -> TableSchema:
        for s in self.schemas:
            if s.name == name:
                return s
        raise KeyError(f"未注册的 schema: {name}")

    # ---- 识别 ----
    def detect(self, header: Sequence[str], rows: Sequence[Sequence[str]] = ()) -> Detection:
        """T2 声明式匹配; 未命中则给出 T1 兜底角色推断。"""
        mapped_by_schema: list[tuple[TableSchema, dict[int, str]]] = [
            (s, s.map_header(header)) for s in self.schemas
        ]
        for schema, mapped in mapped_by_schema:
            present = set(mapped.values())
            for group in schema.require:
                if group and all(f in present for f in group):
                    return Detection(
                        header=[_clean_cell(h) for h in header],
                        signature=signature_of(header),
                        schema=schema,
                        mapped=mapped,
                        roles=infer_roles(header, rows),
                        reason=f"T2 命中 {schema.name} (require={list(group)})",
                    )
        partial = max(
            (len(m) for _s, m in mapped_by_schema),
            default=0,
        )
        return Detection(
            header=[_clean_cell(h) for h in header],
            signature=signature_of(header),
            schema=None,
            mapped={},
            roles=infer_roles(header, rows),
            reason=(
                f"T2 未命中 (最多只映射到 {partial} 列); "
                f"T1 兜底角色: "
                + ", ".join(f"{r.header or r.index}={r.role}" for r in infer_roles(header, rows))
            ),
        )

    # ---- 行映射 ----
    def map_row(self, schema: TableSchema, header: Sequence[str], cells: Sequence[str]) -> dict:
        strategy = ROW_STRATEGIES[schema.row_strategy]
        return strategy(schema.map_header(header), len(header), cells)


def load_registry(path: str | Path = DEFAULT_SCHEMA_PATH) -> SchemaRegistry:
    return SchemaRegistry.load(path)
