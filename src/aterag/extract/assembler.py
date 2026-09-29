"""缝④ 语义装配器: 结构化行 -> 输入条件(外部状态) / 输出条件(自身信号状态).

产测条件的语义定义 (需求方口径):
  输入条件 = 产品行为所依赖的外部状态 (供电输入/环境温度/负载配置/遥控命令/故障激励)
  输出条件 = 产品自身要产生的信号状态 (电压轨/告警信号/遥测值/保护动作/时序行为)

装配只做确定性切分, 分三层来源, 全部标注来源与置信度:
  1. 限值列    -> 响应判据 (4.3.1 输入特性除外: 那里限值本身就是激励域)
  2. 专有列    -> signal_req(告警行为) / range_text+accuracy(遥测)
  3. 备注短语  -> 规则库正则切分 (额定220Vac输入 / 50%最大输出负载 / 温度≥-25℃ ...)
  以上都切不出的, 走人工注记 (annotations/) 或进 needs_review —— 运行时不猜。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aterag.extract.models import (
    CONF_ANNOTATED,
    ROLE_INPUT_DOMAIN,
    ROLE_PROTECTION,
    SRC_ANNOTATION,
    SRC_LIMITS,
    SRC_NOTES,
    SRC_SIGNAL_REQ,
    SRC_TITLE,
    ConditionClause,
)

DEFAULT_PATTERNS_PATH = "config/condition_patterns.yaml"

_OP_MAP = {"≥": ">=", "≤": "<=", "<": "<", ">": ">", "=~": "~"}

# 参与指纹的字段: 决定"人工注记是否已过期"
_FINGERPRINT_FIELDS = (
    "req_id",
    "title",
    "section_path",
    "unit",
    "min",
    "typ",
    "max",
    "rail",
    "priority",
    "notes",
    "subject",
    "signal_name",
    "signal_req",
    "range_text",
    "accuracy",
    "requirement_text",
)


def _norm(v: Any) -> str:
    return str(v or "").strip()


def _clean_text(v: Any) -> str:
    return _norm(v).replace("**", "").replace("<br>", " ")


def row_fingerprint(props: Mapping[str, Any]) -> str:
    """行内容指纹: 文档改版后注记自动失效 (hash 变化 -> 重回待审队列)。"""
    payload = {k: props.get(k) for k in _FINGERPRINT_FIELDS}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------- 规则库 ----------------


@dataclass
class PatternRule:
    id: str
    role: str
    kind: str
    regex: re.Pattern[str]
    capture: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class TitleRule:
    id: str
    regex: re.Pattern[str]
    input_kind: str = ""
    output_kind: str = ""


@dataclass
class TitleKindRule:
    """按条目名判定"这是什么量" —— 单位列为 "-" 时无法从单位推断的兜底。"""

    id: str
    regex: re.Pattern[str]
    kind: str


@dataclass
class PatternBook:
    kinds_input: tuple[str, ...]
    kinds_output: tuple[str, ...]
    limit_kinds: Mapping[str, str]
    rules: tuple[PatternRule, ...]
    title_rules: tuple[TitleRule, ...] = ()
    title_kinds: tuple[TitleKindRule, ...] = ()
    source_path: str = ""

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATTERNS_PATH) -> PatternBook:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"条件规则库不存在: {p} (条件装配的唯一来源)")
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        kinds = doc.get("kinds") or {}
        kin, kout = tuple(kinds.get("input") or ()), tuple(kinds.get("output") or ())
        rules = tuple(
            PatternRule(
                id=r["id"],
                role=r.get("role", "input"),
                kind=r["kind"],
                regex=re.compile(r["pattern"]),
                capture=r.get("capture") or {},
            )
            for r in (doc.get("rules") or [])
        )
        titles = tuple(
            TitleRule(
                id=t["id"],
                regex=re.compile(t["pattern"]),
                input_kind=t.get("input_kind", ""),
                output_kind=t.get("output_kind", ""),
            )
            for t in (doc.get("title_rules") or [])
        )
        tks = tuple(
            TitleKindRule(
                id=t.get("id", f"title_kind_{i}"),
                regex=re.compile(t["pattern"]),
                kind=t["kind"],
            )
            for i, t in enumerate(doc.get("title_kinds") or [])
        )
        book = cls(
            kinds_input=kin,
            kinds_output=kout,
            limit_kinds=dict(doc.get("limit_kinds") or {}),
            rules=rules,
            title_rules=titles,
            title_kinds=tks,
            source_path=str(p),
        )
        book.validate()
        return book

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(self.kinds_input) | frozenset(self.kinds_output)

    def validate(self) -> None:
        """封闭词表自检: 规则引用了未登记的 kind 立刻报错, 而非静默产出下游无法聚合的值。"""
        bad: list[str] = []
        for r in self.rules:
            if r.kind not in self.kinds:
                bad.append(f"rules[{r.id}].kind={r.kind}")
            if r.role not in {"input", "output"}:
                bad.append(f"rules[{r.id}].role={r.role}")
        for t in self.title_rules:
            for attr in ("input_kind", "output_kind"):
                v = getattr(t, attr)
                if v and v not in self.kinds:
                    bad.append(f"title_rules[{t.id}].{attr}={v}")
        for t in self.title_kinds:
            if t.kind not in self.kinds:
                bad.append(f"title_kinds[{t.id}].kind={t.kind}")
        for unit, kind in self.limit_kinds.items():
            if kind not in self.kinds:
                bad.append(f"limit_kinds[{unit}]={kind}")
        if bad:
            raise ValueError(
                "条件规则库引用了 kinds 词表之外的值 (会导致下游无法按 kind 聚合): "
                + "; ".join(bad)
            )


# ---------------- 人工注记 (annotations) ----------------


@dataclass
class AnnotationBook:
    entries: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    source_path: str = ""
    stale: frozenset[str] = frozenset()

    @classmethod
    def load(cls, path: str | Path) -> AnnotationBook:
        p = Path(path)
        if not p.exists():
            return cls(source_path=str(p))
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(entries=doc.get("entries") or {}, source_path=str(p))

    def lookup(self, req_id: str, fingerprint: str) -> tuple[Mapping[str, Any] | None, bool]:
        """返回 (注记, 是否新鲜)。指纹不匹配即视为过期 —— 文档改版后不得沿用旧注记。"""
        e = self.entries.get(req_id)
        if not e:
            return None, False
        stored = str(e.get("fingerprint", ""))
        return e, (stored == fingerprint)


# ---------------- 装配 ----------------


@dataclass
class Assembly:
    inputs: list[ConditionClause] = field(default_factory=list)
    outputs: list[ConditionClause] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    annotated: bool = False
    stale_annotation: bool = False


def _capture_value(rule: PatternRule, m: re.Match[str]) -> dict[str, Any] | None:
    c = rule.capture
    if not c:
        return None
    val: Any = None
    if "value" in c:
        val = c["value"]
    elif "group" in c:
        g = int(c["group"])
        if g > (m.re.groups or 0):
            return None
        try:
            num = float(m.group(g))
        except (TypeError, ValueError):
            num = m.group(g)
        val = num
    if val is None:
        return None
    out: dict[str, Any] = {"value": val}
    if "group2" in c:
        g2 = int(c["group2"])
        if g2 <= (m.re.groups or 0):
            try:
                out["value2"] = float(m.group(g2))
            except (TypeError, ValueError):
                out["value2"] = m.group(g2)
    if c.get("unit"):
        out["unit"] = c["unit"]
    if c.get("op"):
        out["op"] = c["op"]
    if c.get("op_from") and m.re.groups:
        raw = m.group(1) if m.re.groups else ""
        if raw:
            out["op"] = _OP_MAP.get(raw, raw)
    return out


def _limits_value(row: Mapping[str, Any], unit: str) -> dict[str, Any] | None:
    """限值 -> 结构化值。短横线已在行映射阶段转为 None, 这里只处理"至少一端有值"。"""
    lo, typ, hi = row.get("min"), row.get("typ"), row.get("max")
    if lo is None and typ is None and hi is None:
        return None
    v: dict[str, Any] = {}
    if lo is not None:
        v["min"] = lo
    if typ is not None:
        v["typ"] = typ
    if hi is not None:
        v["max"] = hi
    if unit:
        v["unit"] = unit
    return v or None


def _limits_text(row: Mapping[str, Any], unit: str) -> str:
    """限值文本: 只描述实际给出数值的维度, 缺失维度不虚构 (短横线=无数据)。"""
    lo, typ, hi = row.get("min"), row.get("typ"), row.get("max")
    if lo is not None and hi is not None:
        s = f"{lo}~{hi}"
    elif typ is not None:
        s = f"{typ}"
    else:
        s = f"{lo if lo is not None else hi}"
    return f"{s} {unit}".strip()


def _dedupe(clauses: Sequence[ConditionClause]) -> list[ConditionClause]:
    out: list[ConditionClause] = []
    seen: set[tuple[str, str, str]] = set()
    for c in clauses:
        key = (c.role, c.kind, c.text)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def assemble(
    row: Mapping[str, Any],
    *,
    role: str,
    limits_to: str,
    book: PatternBook,
    annotations: AnnotationBook | None = None,
) -> Assembly:
    """把一行需求装配成 输入条件 / 输出条件 子句对。"""
    asm = Assembly()
    fp = row_fingerprint(row)

    # 1) 人工注记优先 (已审核的语义高于规则推断)
    if annotations is not None:
        entry, fresh = annotations.lookup(_norm(row.get("req_id")), fp)
        if entry is not None:
            if not fresh:
                asm.stale_annotation = True
                asm.flags.append("annotation_stale")
            else:
                asm.annotated = True
                for spec in entry.get("input") or []:
                    asm.inputs.append(
                        _clause_from_spec(spec, "input", SRC_ANNOTATION, CONF_ANNOTATED)
                    )
                for spec in entry.get("output") or []:
                    asm.outputs.append(
                        _clause_from_spec(spec, "output", SRC_ANNOTATION, CONF_ANNOTATED)
                    )
                if asm.inputs or asm.outputs:
                    return asm

    title = _clean_text(row.get("title"))
    unit = _clean_text(row.get("unit"))
    rail = _clean_text(row.get("rail"))

    # 2) 限值列: 归到激励侧还是响应侧由章节先验决定; "是什么量"由 limit_kinds 决定。
    #    两者都定不下来时只打标记, 不猜 —— 宁可少一条子句, 也不产出错的语义标签。
    limits = _limits_value(row, unit)
    limit_kind = book.limit_kinds.get(unit, "")
    if limits is not None and not limit_kind:
        # 单位列写 "-" (无量纲) 或缺省时, 改由条目名判定是什么量
        for tk in book.title_kinds:
            if tk.regex.search(title):
                limit_kind = tk.kind
                asm.flags.append("limit_kind_from_title")
                break
    if limits is not None and not limit_kind:
        asm.flags.append("limit_kind_unmapped")
    if limits is not None and limit_kind:
        text = f"{title}: {_limits_text(row, unit)}".strip(": ")
        if limits_to in {"input", "both"}:
            asm.inputs.append(
                ConditionClause(
                    kind="fault_stimulus" if role == ROLE_PROTECTION else limit_kind,
                    text=text,
                    role="input",
                    value=limits,
                    source=SRC_LIMITS,
                )
            )
        if limits_to in {"output", "both"}:
            asm.outputs.append(
                ConditionClause(
                    kind="protection_action" if role == ROLE_PROTECTION else limit_kind,
                    text=text,
                    role="output",
                    value=limits,
                    source=SRC_LIMITS,
                )
            )

    # 3) 专有列: 告警行为 / 遥测量
    # 信号名只是标识, 单独成条属噪声 —— 合并进描述, 只产出一条。
    sig_req = _clean_text(row.get("signal_req"))
    sig_name = _clean_text(row.get("signal_name"))
    if sig_req or sig_name:
        asm.outputs.append(
            ConditionClause(
                kind="signal_state",
                text=f"{sig_name}: {sig_req}" if (sig_name and sig_req) else (sig_req or sig_name),
                role="output",
                source=SRC_SIGNAL_REQ,
            )
        )
    rng = _clean_text(row.get("range_text"))
    acc = _clean_text(row.get("accuracy"))
    if rng or acc:
        subj = _clean_text(row.get("subject")) or title
        asm.outputs.append(
            ConditionClause(
                kind="telemetry_value",
                text=f"{subj}: 检测范围 {rng}{(' 精度 ' + acc) if acc else ''}".strip(),
                role="output",
                value={"range": rng, "accuracy": acc} if (rng or acc) else None,
                source=SRC_SIGNAL_REQ,
            )
        )
    req_text = _clean_text(row.get("requirement_text"))
    if req_text:
        asm.outputs.append(
            ConditionClause(kind="presence", text=req_text, role="output", source=SRC_NOTES)
        )

    # 4) 标题语义: 保护点/告警 -> 故障激励 + 动作/信号
    for t in book.title_rules:
        m = t.regex.match(title)
        if not m:
            continue
        if t.input_kind:
            asm.inputs.append(
                ConditionClause(
                    kind=t.input_kind,
                    text=f"{title} (触发条件)",
                    role="input",
                    value={"event": title},
                    source=SRC_TITLE,
                )
            )
        if t.output_kind:
            asm.outputs.append(
                ConditionClause(
                    kind=t.output_kind,
                    text=f"{title} (期望响应)",
                    role="output",
                    value=None,
                    source=SRC_TITLE,
                )
            )
        break

    # 5) 备注短语: 规则库切分激励条件
    notes = _clean_text(row.get("notes"))
    if notes and notes not in {"-", "—", "/"}:
        for rule in book.rules:
            for m in rule.regex.finditer(notes):
                text = _clean_text(m.group(0))
                if not text:
                    continue
                clause = ConditionClause(
                    kind=rule.kind,
                    text=text,
                    role=rule.role,
                    value=_capture_value(rule, m),
                    source=SRC_NOTES,
                )
                (asm.inputs if rule.role == "input" else asm.outputs).append(clause)

    asm.inputs = _dedupe(asm.inputs)
    asm.outputs = _dedupe(asm.outputs)

    if not asm.inputs:
        asm.flags.append("no_input_condition")
    if not asm.outputs:
        asm.flags.append("no_output_condition")
    if role == ROLE_INPUT_DOMAIN and limits is None:
        asm.flags.append("input_domain_without_limits")
    if rail:
        asm.flags.append("multi_rail")
    return asm


def _clause_from_spec(
    spec: Mapping[str, Any], role: str, source: str, confidence: str
) -> ConditionClause:
    return ConditionClause(
        kind=_norm(spec.get("kind")) or "presence",
        text=_clean_text(spec.get("text")),
        role=role,
        value=dict(spec["value"]) if isinstance(spec.get("value"), Mapping) else None,
        source=source,
        confidence=confidence,
    )
