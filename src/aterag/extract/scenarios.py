"""条件场景拆分 (A7) —— 把一条需求展开成多个可执行测试场景。

为什么
----
产测用例不是"一条需求一个用例", 而是"一条需求 × N 个条件组合"。同一个"输出电流"
需求在 110Vac 与 230Vac 下满载电流不同 —— 110Vac 下受输出功率档封顶, 满载只有
7.401A 而非额定 11.1A。不拆场景就只有一个含混判据, 产测按哪个值执行都可能是错的。

边界 (与其它模块的分工)
------------------------
* 只展开"条件组合", 不生成用例、不写脚本、不排序流程 —— 那些是 P2/P2.5 的职责。
* 不推算规格书没给的限值。场景绑定"在什么条件下测", 判据仍来自规格书。
* 不可行组合显式排除并给理由, 进入 excluded 桶, 不静默丢弃(与三桶原则一致)。

安全: 档位公式用受限求值器(仅四则与括号), 不用 eval —— 配置是可提交到仓库的
文本, 一旦允许任意表达式就等于开了代码执行的口子。
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aterag.extract.models import TestCondition

DEFAULT_RULES_PATH = Path("config/scenario_rules.yaml")

#: 语义常量: 用于按"输出电流"语义筛选额定电流。kind 与单位名取自
#: condition_patterns.yaml 的封闭词表 —— 这里只做等值比较, 不引入新的词汇。
_KIND_OUTPUT_CURRENT = "output_current"
_KIND_LOAD = "load"
_KIND_INPUT_VOLTAGE = "input_voltage"
_KIND_DUTY = "duty"
#: 补齐层来源标记 (见 models.SRC_METHOD) —— 补进来的通用判据不作为命名依据。
_SRC_INDUSTRY_METHOD = "industry_method"
_UNIT_CURRENT = "A"

#: 语义常量: 限值的三个形态键(与 TestCondition.limits 的键同名)。
_LIMIT_KEYS = ("min", "typ", "max")

#: 名称模板可用的渲染上下文 (键固定, 避免模板引用不存在变量)。
_NAME_CTX = frozenset({"value_g", "min_g", "max_g", "value", "rail", "unit"})


@dataclass(frozen=True, slots=True)
class NamingRule:
    """一条命名规则: when 命中即产出名称片段。"""

    id: str
    when: Mapping[str, Any] = field(default_factory=dict)
    name: str = ""
    name_pattern: str = ""

    def render(self, ctx: Mapping[str, Any]) -> str:
        if self.name_pattern:
            return self.name_pattern.format(**ctx)
        return self.name


@dataclass(frozen=True, slots=True)
class NamingBook:
    """条件命名规则集 (声明在 scenario_rules.yaml 的 naming 段)。

    为什么命名也要外置: "满载输出电流""输入最大值"这类措辞是产测行业的
    通用叫法, 属于文档/行业认知而非代码逻辑。写进代码就等于把某一套叫法
    焊死在本实现, 换行业或换客户叫法时要改代码。
    """

    subjects: tuple[NamingRule, ...] = ()
    load_levels: tuple[NamingRule, ...] = ()
    load_levels_absolute: tuple[NamingRule, ...] = ()
    input_levels: tuple[NamingRule, ...] = ()
    duty_levels: tuple[NamingRule, ...] = ()
    scenario_name: str = "{prefix}{subject}{rail_suffix}"
    rail_suffix_pattern: str = "@{rail}"
    #: 输入侧条目命中输入前缀时只输出前缀, 不拼接输出对象
    input_only_no_subject: bool = True
    #: 负载档命名要求单位为百分比 (见 config 注释: 速率类 load 不可当百分比)
    load_levels_require_unit_percent: bool = True

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> NamingBook:
        n = doc.get("naming") or {}

        def rules(key: str) -> tuple[NamingRule, ...]:
            return tuple(
                NamingRule(
                    id=str(x.get("id", "")),
                    when=dict(x.get("when") or {}),
                    name=str(x.get("name", "")),
                    name_pattern=str(x.get("name_pattern", "")),
                )
                for x in (n.get(key) or [])
            )

        return cls(
            subjects=rules("subjects"),
            load_levels=rules("load_levels"),
            load_levels_absolute=rules("load_levels_absolute"),
            input_levels=rules("input_levels"),
            duty_levels=rules("duty_levels"),
            scenario_name=str(n.get("scenario_name", "{prefix}{subject}{rail_suffix}")),
            rail_suffix_pattern=str(n.get("rail_suffix_pattern", "@{rail}")),
            input_only_no_subject=bool(n.get("input_only_no_subject", True)),
            load_levels_require_unit_percent=bool(n.get("load_levels_require_unit_percent", True)),
        )

    def validate(self) -> None:
        bad: list[str] = []
        if not self.subjects:
            bad.append("naming.subjects 为空 (场景名将无法给出被测对象)")
        for group, items in (
            ("load_levels", self.load_levels),
            ("input_levels", self.input_levels),
            ("duty_levels", self.duty_levels),
            ("load_levels_absolute", self.load_levels_absolute),
        ):
            for r in items:
                if not r.name and not r.name_pattern:
                    bad.append(f"naming.{group}[{r.id}] 既无 name 也无 name_pattern")
                if not r.when:
                    # 空 when 会匹配一切 -> 规则退化成"永远第一个赢",
                    # 表现为所有条目都被命名成同一个词。必须当场拒绝。
                    bad.append(f"naming.{group}[{r.id}] 的 when 为空 (会匹配所有需求)")
                for token in re.findall(r"\{([a-z_]+)\}", r.name_pattern):
                    if token not in _NAME_CTX:
                        bad.append(f"naming.{group}[{r.id}] 使用了未知占位符 {{{token}}}")
        for r in self.subjects:
            if not r.when:
                bad.append(f"naming.subjects[{r.id}] 的 when 为空 (会匹配所有需求)")
            if not r.name and not r.name_pattern:
                bad.append(f"naming.subjects[{r.id}] 既无 name 也无 name_pattern")
        if bad:
            raise ValueError("条件命名规则不自洽: " + "; ".join(bad))


# 受限求值: 只允许数字与四则运算/一元正负/括号。不含变量名以外的一切。
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


@dataclass(frozen=True, slots=True)
class Tier:
    """一个输入电压档与其输出功率上限。

    来自规格书原文(如 SR-1204 备注 "90~176Vac: 400W"), 不由代码臆造。
    """

    min_vac: float
    max_vac: float
    power_w: float
    source_text: str = ""

    def contains(self, vac: float) -> bool:
        return self.min_vac <= vac <= self.max_vac

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_vac": self.min_vac,
            "max_vac": self.max_vac,
            "power_w": self.power_w,
            "source_text": self.source_text,
        }


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    """一个可展开的条件维度 (从规格书解析取值)。"""

    key: str
    label: str = ""
    unit_hint: str = ""
    pattern: str = ""
    from_notes: bool = True


@dataclass(frozen=True, slots=True)
class LoadDerivation:
    """轨级负载推导规则 (功率档封顶)。"""

    id: str
    basis: str
    formula: str
    priority_rails: tuple[str, ...] = ()
    tolerance: float = 1e-3
    regression_golden: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ScenarioRules:
    """场景规则集 (声明在 config/scenario_rules.yaml)。"""

    dimensions: tuple[DimensionSpec, ...] = ()
    tier_pattern: str = ""
    tier_from_notes: bool = True
    tier_from_requirement_title: str = ""
    derivations: tuple[LoadDerivation, ...] = ()
    dimension_order: tuple[str, ...] = ()
    sort_ascending: bool = True
    naming: NamingBook = field(default_factory=NamingBook)
    path: str = ""

    @classmethod
    def load(cls, path: str | Path = DEFAULT_RULES_PATH) -> ScenarioRules:
        p = Path(path)
        if not p.exists():
            # fail-closed: 规则缺失时不能静默退化成"不拆场景" ——
            # 那会让下游拿到含混判据却不报错, 是最难排查的一类问题。
            raise FileNotFoundError(f"场景规则不存在: {p}")
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        tiers = doc.get("tiers") or {}
        parse = tiers.get("parse") or {}
        order = doc.get("ordering") or {}
        return cls(
            dimensions=tuple(
                DimensionSpec(
                    key=str(d.get("key", "")),
                    label=str(d.get("label", "")),
                    unit_hint=str(d.get("unit_hint", "")),
                    pattern=str(d.get("pattern", "")),
                    from_notes=bool(d.get("from_notes", True)),
                )
                for d in (doc.get("dimensions") or [])
            ),
            tier_pattern=str(parse.get("pattern", "")),
            tier_from_notes=bool(parse.get("from_notes", True)),
            tier_from_requirement_title=str(parse.get("from_requirement_title", "")),
            derivations=tuple(
                LoadDerivation(
                    id=str(x.get("id", "")),
                    basis=str(x.get("basis", "")),
                    formula=str(x.get("formula", "")),
                    priority_rails=tuple(str(r) for r in (x.get("priority_rails") or ())),
                    tolerance=float(x.get("tolerance", 1e-3)),
                    regression_golden=tuple(x.get("regression_golden") or ()),
                )
                for x in (doc.get("load_derivation") or [])
            ),
            dimension_order=tuple(str(k) for k in (order.get("dimension_order") or ())),
            sort_ascending=bool(order.get("sort_numeric_ascending", True)),
            naming=NamingBook.from_doc(doc),
            path=str(p),
        )

    def validate(self) -> None:
        bad: list[str] = []
        self.naming.validate()
        if not self.dimensions:
            bad.append("scenario_rules.yaml 未定义任何 dimension")
        if not self.tier_pattern:
            bad.append("scenario_rules.yaml 未定义 tiers.parse.pattern")
        for d in self.dimensions:
            if d.pattern:
                try:
                    re.compile(d.pattern)
                except re.error as e:
                    bad.append(f"dimension[{d.key}].pattern 正则非法: {e}")
        for t in self.tier_pattern and [self.tier_pattern] or []:
            try:
                re.compile(t)
            except re.error as e:
                bad.append(f"tiers.parse.pattern 正则非法: {e}")
        for dv in self.derivations:
            if not dv.formula:
                bad.append(f"load_derivation[{dv.id}] 缺 formula")
            if not dv.basis:
                bad.append(f"load_derivation[{dv.id}] 缺 basis (推导须有依据)")
            try:
                # 探测变量从公式里实际用到的名字自动取 1.0 —— 写死一份变量表会
                # 和公式一起漂移(公式加了个变量而探测表没加, 校验就形同虚设)。
                _safe_eval(dv.formula, {v: 1.0 for v in _formula_vars(dv.formula)})
            except ValueError as e:
                bad.append(f"load_derivation[{dv.id}].formula 不合法: {e}")
        if bad:
            raise ValueError("场景规则不自洽: " + "; ".join(bad))


def _is_percent_unit(unit: Any) -> bool:
    """单位是否表示百分比。

    规格书里同一语义会写成 "% 最大输出" / "% 额定" / "%" 等多种写法,
    故按"含百分号"判定, 而不是枚举全部写法 —— 枚举必然漏, 漏了就会
    把非百分比语义的数值当成负载比例去命名。
    """
    return isinstance(unit, str) and "%" in unit


def _load_clause(cond: TestCondition) -> Any:
    """取负载子句 (无则 None)。"""
    for cl in cond.input_conditions:
        if cl.kind == _KIND_LOAD:
            return cl
    return None


def _input_clause(cond: TestCondition) -> Any:
    for cl in cond.input_conditions:
        if cl.kind == _KIND_INPUT_VOLTAGE:
            return cl
    return None


def _duty_clause(cond: TestCondition) -> Any:
    for cl in cond.input_conditions:
        if cl.kind == _KIND_DUTY:
            return cl
    return None


def _numeric_value(cl: Any) -> float | None:
    """从子句 value 里取数值 (百分数/绝对值, 取第一个数值型键)。"""
    if cl is None or not cl.value:
        return None
    for k in ("value", "min", "typ", "max"):
        v = cl.value.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _match_naming(rule: NamingRule, ctx: Mapping[str, Any]) -> bool:
    """命名规则的 when 判定。

    支持的键: mode / value_eq / value_gte / value_pct_range / value_pct_any /
    limits_only / limits_all / unit / has_value / title_pattern / value / kinds
    """
    w = rule.when
    if "value" in w and ctx.get("raw_value") != w["value"]:
        return False
    if "mode" in w and ctx.get("mode") != w["mode"]:
        return False
    if "unit" in w and ctx.get("unit") != w["unit"]:
        return False
    if "unit_is" in w:
        # 限定量纲: 电压类命名规则只对电压单位生效。不加这道闸, 带输入电压
        # 分档备注的输出功率行(0~600W)会被命名成"输入范围0~600Vac",
        # 产测照此设输入电压会直接损坏产品。
        units = w["unit_is"] if isinstance(w["unit_is"], list) else [w["unit_is"]]
        if ctx.get("unit") not in units:
            return False
    if "title_pattern" in w and not re.search(str(w["title_pattern"]), str(ctx.get("title", ""))):
        return False
    if "kinds" in w and ctx.get("subject_kind") not in set(w["kinds"]):
        return False
    if "has_value" in w and bool(ctx.get("has_value")) != bool(w["has_value"]):
        return False
    if "limits_only" in w and sorted(ctx.get("limit_keys") or []) != sorted(w["limits_only"]):
        return False
    if "limits_all" in w and not set(w["limits_all"]) <= set(ctx.get("limit_keys") or []):
        return False
    val = ctx.get("value")
    if "value_eq" in w and (val is None or abs(val - float(w["value_eq"])) > 1e-9):
        return False
    if "value_gte" in w and (val is None or val < float(w["value_gte"]) - 1e-9):
        return False
    if "value_pct_range" in w:
        lo, hi = w["value_pct_range"]
        if val is None or not (float(lo) - 1e-9 <= val <= float(hi) + 1e-9):
            return False
    if "value_pct_any" in w and val is None:
        return False
    return True


def _bound_text(cond: TestCondition, cl: Any, key: str) -> str:
    """取上/下限的显示值: 优先子句 value, 回落需求限值。

    必须回落 cond.limits —— 输入电压类的上下限多数记在限值列而非子句 value,
    只看子句会渲染出空的"输入范围~Vac"。
    """
    if cl is not None and cl.value and key in cl.value:
        v = cl.value[key]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return f"{float(v):g}"
    lim = (cond.limits or {}).get(key)
    if isinstance(lim, (int, float)) and not isinstance(lim, bool):
        return f"{float(lim):g}"
    return ""


def _name_ctx(cl: Any, cond: TestCondition, kind: str = "") -> dict[str, Any]:
    """构造名称模板的渲染上下文。"""
    val = _numeric_value(cl)
    unit = (cl.value or {}).get("unit") if cl is not None and cl.value else None
    return {
        "value_g": f"{val:g}" if val is not None else "",
        "value": val if val is not None else 0,
        "min_g": _bound_text(cond, cl, "min"),
        "max_g": _bound_text(cond, cl, "max"),
        "mode": (cl.value or {}).get("mode") if cl is not None and cl.value else None,
        "unit": unit or (cond.limits or {}).get("unit"),
        "raw_value": (cl.value or {}).get("value") if cl is not None and cl.value else None,
        "has_value": val is not None,
        "limit_keys": [k for k in _LIMIT_KEYS if (cond.limits or {}).get(k) is not None],
        "title": cond.title or "",
        "subject_kind": kind,
        "rail": cond.rail or "",
    }


def derive_condition_name(cond: TestCondition, naming: NamingBook) -> str:
    """给一条需求算出产测可读名称, 例 "满载输出电流"。

    结构: 工况前缀(负载/输入档/工作制, 首个命中) + 被测对象。
    输入侧条目没有"被测对象" —— 它的被测量就是输入本身, 故只给前缀名。
    """
    # 被测对象: 取输出侧子句的 kind 逐个匹配 subjects 规则。
    # 逐个(而非只看第一个)的原因: 一条需求的输出侧常同时有多个 kind
    # (如"输出电流"行会带 output_current + presence), 取第一个会漏掉真正的
    # 被测量, 退化成标题兜底, 于是所有条目都被命名成同一个词。
    subject = ""
    # 只认"规格书原有"的输出子句作为被测对象依据。补齐层加进来的通用判据
    # (如"各路输出电压应落在额定范围")不是本条目的被测量 ——
    # 拿它命名会把"功率因数"标成"输出电压", 产测照着名字做会测错东西。
    out_kinds = [c.kind for c in cond.output_conditions if c.source != _SRC_INDUSTRY_METHOD]
    for kind in out_kinds:
        for r in naming.subjects:
            ctx = _name_ctx(None, cond, kind=kind)
            if _match_naming(r, ctx):
                subject = r.render(ctx)
                break
        if subject:
            break
    if not subject:
        # 标题即被测对象(如"功率因数""峰峰值杂音电压"), 优先于通用兜底词
        subject = cond.title or ""

    # 工况前缀: 负载 -> 输入档 -> 工作制, 首个命中。
    # 记录命中的组: 输入档命中的条目其被测对象就是输入本身, 不该再拼输出对象。
    prefix = ""
    prefix_group = ""
    load = _load_clause(cond)
    if load is not None:
        ctx = _name_ctx(load, cond)
        # 单位闸: 负载命名只适用于"百分比"语义的 load 子句。动态响应的
        # load 子句是速率(0.1A/uS), 直接按百分比命名会造出"0.1%载…"这种
        # 既不存在的工况、又丢掉速率语义的名字。
        unit_ok = not naming.load_levels_require_unit_percent or _is_percent_unit(ctx.get("unit"))
        for r in (*naming.load_levels_absolute, *naming.load_levels):
            if unit_ok and _match_naming(r, ctx):
                prefix = r.render(ctx)
                prefix_group = "load"
                break
    if not prefix:
        inp = _input_clause(cond)
        if inp is not None:
            ctx = _name_ctx(inp, cond)
            for r in naming.input_levels:
                if _match_naming(r, ctx):
                    prefix = r.render(ctx)
                    prefix_group = "input"
                    break
    if not prefix:
        duty = _duty_clause(cond)
        if duty is not None:
            ctx = _name_ctx(duty, cond)
            for r in naming.duty_levels:
                if _match_naming(r, ctx):
                    prefix = r.render(ctx)
                    prefix_group = "duty"
                    break
    if naming.input_only_no_subject and prefix_group == "input":
        return prefix
    return f"{prefix}{subject}"


def derive_scenario_name(cond: TestCondition, naming: NamingBook, rail: str = "") -> str:
    """场景名 = 条件名 + 轨后缀 (同一条需求在不同轨上是不同的测点)。"""
    base = derive_condition_name(cond, naming)
    if not rail:
        return base
    return f"{base}{naming.rail_suffix_pattern.format(rail=rail)}"


def _formula_vars(expr: str) -> set[str]:
    """取出公式里用到的变量名 (受限: 只认 ast.Name)。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _safe_eval(expr: str, vars_: Mapping[str, float]) -> float:
    """受限表达式求值 —— 只允许数字/变量/四则/括号。

    不用 eval: 配置文件是入库代码, 允许任意表达式等于开了执行入口。
    这里显式白名单 AST 节点, 遇到任何其它节点直接拒绝。
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误: {e}") from e

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError(f"只允许数值常量, 得到 {node.value!r}")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in vars_:
                raise ValueError(f"未知变量 {node.id!r}")
            return float(vars_[node.id])
        if isinstance(node, ast.BinOp):
            op = _BIN_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"不支持的运算符 {type(node.op).__name__}")
            return op(walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp):
            op = _UNARY_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"不支持的一元运算 {type(node.op).__name__}")
            return op(walk(node.operand))
        raise ValueError(f"表达式含不允许的语法: {type(node).__name__}")

    return walk(tree)


@dataclass(frozen=True, slots=True)
class Scenario:
    """一个可执行条件组合。

    Attributes:
        scenario_id: 稳定标识 (需求编号 + 维度值), 顺序无关
        seq: 稳定序号 —— case_code 由此派生, 顺序必须可复现
        req_id / title / rail: 所属需求
        bindings: 维度取值 {"ac_input_tier": "90~176Vac"}
        derived: 推导结果 {"-54V": 7.401} (如有)
        basis: 推导依据 (可追到标准与规格书原文)
    """

    scenario_id: str
    seq: int
    req_id: str
    title: str
    rail: str
    #: 产测可读名称, 例 "满载输出电流@3.45V"。
    #: P2 由此生成用例名 —— 产测人员看的是这个名字, 不是 kind 碎片。
    name: str = ""
    bindings: dict[str, str] = field(default_factory=dict)
    derived: dict[str, float] = field(default_factory=dict)
    basis: str = ""
    source: str = "spec"  # spec=规格书分档 | derived=按功率档推导

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "seq": self.seq,
            "req_id": self.req_id,
            "title": self.title,
            "rail": self.rail,
            "name": self.name,
            "bindings": self.bindings,
            "derived": self.derived,
            "basis": self.basis,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ExcludedScenario:
    """被排除的组合 —— 必须带理由(与 ExcludedItem 同一原则)。"""

    req_id: str
    title: str
    bindings: dict[str, str]
    reason: str
    rule_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "req_id": self.req_id,
            "title": self.title,
            "bindings": self.bindings,
            "reason": self.reason,
            "rule_id": self.rule_id,
        }


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """场景拆分产出。"""

    scenarios: list[Scenario] = field(default_factory=list)
    excluded: list[ExcludedScenario] = field(default_factory=list)
    tiers: list[Tier] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "scenarios": len(self.scenarios),
            "excluded": len(self.excluded),
            "tiers": len(self.tiers),
        }


def parse_tiers(rules: ScenarioRules, conditions: Sequence[TestCondition]) -> list[Tier]:
    """从规格书原文解析输入电压档 —— 代码只认正则, 数字全部来自原文。"""
    if not rules.tier_pattern:
        return []
    rx = re.compile(rules.tier_pattern)
    out: list[Tier] = []
    seen: set[tuple[float, float, float]] = set()
    for c in conditions:
        if rules.tier_from_requirement_title and c.title != rules.tier_from_requirement_title:
            continue
        if not rules.tier_from_notes or not c.notes:
            continue
        for m in rx.finditer(c.notes):
            lo, hi, pw = float(m.group(1)), float(m.group(2)), float(m.group(3))
            key = (lo, hi, pw)
            if key in seen:
                continue
            seen.add(key)
            out.append(Tier(min_vac=lo, max_vac=hi, power_w=pw, source_text=m.group(0)))
    out.sort(key=lambda t: (t.min_vac, t.max_vac))
    return out


def _rated_currents(conditions: Sequence[TestCondition]) -> dict[str, float]:
    """各轨额定满载电流。

    必须按"输出电流"语义筛选, 不能只看 rail + max: 同一电压轨下还有输出电压
    (max=55.62)、温度(-54.8) 等行, 混进来会把额定电流算错一个量级。
    筛选条件: 条件子句里出现输出电流语义, 且限值单位是电流单位。
    """
    out: dict[str, float] = {}
    for c in conditions:
        if not c.rail:
            continue
        mx = c.limits.get("max") if c.limits else None
        if mx is None:
            continue
        # 输出电流语义: 标题或子句 kind 指明是电流
        is_current = any(cl.kind == _KIND_OUTPUT_CURRENT for cl in c.output_conditions) or (
            c.limits.get("unit") == _UNIT_CURRENT
        )
        if not is_current:
            continue
        out.setdefault(c.rail, float(mx))
    return out


def _rail_voltages(conditions: Sequence[TestCondition]) -> dict[str, float]:
    """各轨电压 (由轨名解析, 如 "-54V" -> 54.0)。"""
    out: dict[str, float] = {}
    for c in conditions:
        if not c.rail:
            continue
        m = re.match(r"^-?\s*(\d+(?:\.\d+)?)\s*V", c.rail.strip(), re.IGNORECASE)
        if m:
            out.setdefault(c.rail, abs(float(m.group(1))))
    return out


def _derive_load(
    rules: ScenarioRules,
    tiers: Sequence[Tier],
    rated_a: Mapping[str, float],
    volts: Mapping[str, float],
) -> dict[tuple[float, float, float], dict[str, float]]:
    """按功率档推导各轨满载电流。

    返回 {(min,max,power) -> {rail: current}}。

    推导顺序由配置里的 priority_rails 决定, 语义是"先满足的轨先分配":
      * 中间轨按其额定电流取值, 功耗计入 P_aux(它要先吃掉一部分功率);
      * 最后一轨吃剩余功率 (P - P_aux) / U。
    这样 400W 档下 3.45V 轨先取 0.1A(=0.345W), 主轨得 (400-0.345)/54=7.401A,
    与需求方已确认的口径一致; 顺序反了就会算出 400/54=7.407A 而偏 6mA。
    顺序属规则的一部分(会改变结论), 故由配置声明而非代码固定。
    """
    out: dict[tuple[float, float, float], dict[str, float]] = {}
    der = next((d for d in rules.derivations if d.formula), None)
    if der is None:
        return out
    order = [r for r in der.priority_rails if r in rated_a and r in volts]
    if not order:
        order = sorted(r for r in rated_a if r in volts)
    for t in tiers:
        currents: dict[str, float] = {}
        p_aux = 0.0
        for i, rail in enumerate(order):
            v = volts[rail]
            if i < len(order) - 1:
                # 非末轨: 按额定取值(先满足它)
                cur = rated_a[rail]
            else:
                # 末轨: 吃剩余功率
                cur = _safe_eval(der.formula, {"P": t.power_w, "P_aux": p_aux, "U": v})
                cur = round(cur, 3)
                if cur > rated_a[rail] + der.tolerance:
                    # 推导超过额定值 -> 该档下末轨拿不满, 记为该档不可达
                    cur = rated_a[rail]
            currents[rail] = cur
            p_aux += cur * v
        if currents:
            out[(t.min_vac, t.max_vac, t.power_w)] = currents
    return out


def expand_scenarios(
    conditions: Sequence[TestCondition],
    rules: ScenarioRules,
) -> ScenarioResult:
    """把需求展开成条件场景矩阵。

    排序稳定: 维度按 ordering.dimension_order 声明顺序, 数值维度升序 ——
    case_code 由 seq 派生, 顺序若不稳定, 幂等导入就会重复建用例。
    """
    rules.validate()
    tiers = parse_tiers(rules, conditions)
    rated = _rated_currents(conditions)
    volts = _rail_voltages(conditions)
    derived = _derive_load(rules, tiers, rated, volts) if tiers else {}

    res = ScenarioResult(tiers=tiers)
    # seq 必须"每需求内唯一且连续": P2 的 case_code = f"{req_id}-S{seq:03d}",
    # 同一需求的多个来源行(多轨/多工作制)共享 req_id, 若每个行各自从 0 开始,
    # case_code 就会撞车 -> 幂等导入把多行并成一条, 条件集静默丢失。
    # 故序号按需求维度统一分配, 而不是按行。
    seq_by_req: dict[str, int] = {}
    for c in conditions:
        # 无分档维度可用时, 每条需求一个基线场景(不拆)
        if not tiers:
            res.scenarios.append(
                Scenario(
                    scenario_id=f"{c.req_id}#base",
                    seq=0,
                    req_id=c.req_id,
                    title=c.title,
                    rail=c.rail,
                    name=derive_scenario_name(c, rules.naming, c.rail),
                    basis="no_tier_split",
                    source="spec",
                )
            )
            continue
        seq = seq_by_req.get(c.req_id, 0)
        for t in tiers:
            binding = {
                "ac_input_tier": f"{t.min_vac:g}~{t.max_vac:g}Vac",
            }
            key = (t.min_vac, t.max_vac, t.power_w)
            d = derived.get(key, {})
            for rail in c.rail and [c.rail] or []:
                if rail in d:
                    if d[rail] > rated.get(rail, d[rail]) + 1e-3:
                        res.excluded.append(
                            ExcludedScenario(
                                req_id=c.req_id,
                                title=c.title,
                                bindings=binding,
                                reason="derived_exceeds_rated",
                                rule_id="exclude_beyond_rated",
                            )
                        )
                        continue
                    res.scenarios.append(
                        Scenario(
                            scenario_id=_sid(c.req_id, t, rail, c.notes),
                            seq=seq,
                            req_id=c.req_id,
                            title=c.title,
                            rail=rail,
                            bindings=binding,
                            derived={rail: d[rail]},
                            name=derive_scenario_name(c, rules.naming, rail),
                            basis=f"tier_power_capped:{t.power_w:g}W",
                            source="derived",
                        )
                    )
                    seq += 1
                else:
                    res.excluded.append(
                        ExcludedScenario(
                            req_id=c.req_id,
                            title=c.title,
                            bindings=binding,
                            reason="rail_not_powered_by_tier",
                            rule_id="exclude_cascading_rail_tiers",
                        )
                    )
            if not c.rail:
                res.scenarios.append(
                    Scenario(
                        scenario_id=_sid(c.req_id, t, "", c.notes),
                        seq=seq,
                        req_id=c.req_id,
                        title=c.title,
                        rail="",
                        bindings=binding,
                        name=derive_scenario_name(c, rules.naming, ""),
                        basis=f"tier_power_capped:{t.power_w:g}W",
                        source="spec",
                    )
                )
                seq += 1
        # 该需求的下一行从当前序号继续, 保证 (req_id, seq) 全局唯一
        seq_by_req[c.req_id] = seq
    _assert_unique(res.scenarios)
    return res


def _assert_unique(scenarios: Sequence[Scenario]) -> None:
    """场景标识与 (需求,序号) 必须各自唯一 —— 重复即静默丢数据, 就地失败。

    这条断言不能省: 标识重复时 P2 的 case_code 也会重复, 幂等导入会把多条
    需求合并成一条用例, 条件集悄悄少一半, 而产线只表现为"少数了几个点"。
    """
    ids = [s.scenario_id for s in scenarios]
    if len(ids) != len(set(ids)):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"场景标识重复({len(dup)} 个): {dup[:5]} — 检查 sid 是否含行限定词")
    per_req: dict[str, list[int]] = {}
    for s in scenarios:
        per_req.setdefault(s.req_id, []).append(s.seq)
    bad = {k: v for k, v in per_req.items() if len(v) != len(set(v))}
    if bad:
        raise ValueError(
            f"同需求内场景序号重复: {list(bad)[:5]} — case_code 由 (req_id, seq) 派生, "
            "序号冲突会让幂等导入把多行并成一条用例"
        )


def _sid(req_id: str, tier: Tier, rail: str, notes: str = "") -> str:
    """场景标识。

    必须含 req_id + 档位 + 轨 + 行限定词 四要素:
      * 同一需求可能有多行(多轨拆分、以及同轨不同工作制, 如 SR-1203 有
        -54V 长期/-54V 短期/3.45V 长期三行共用一个 req_id);
      * P2 的 case_code 由本标识派生, 一旦重复, 幂等导入就会把多行合并成
        一条用例, 条件集静默丢失 —— 这是最难在产线上发现的一类错误。
    notes 在此只作区分限定词(内部空白归一), 不改写原文 —— 原文仍由
    TestCondition.notes 原样保留。
    """
    tier_part = f"t{tier.min_vac:g}-{tier.max_vac:g}"
    rail_part = f"@{rail}" if rail else "@na"
    qual = re.sub(r"\s+", "", notes or "")[:12]
    qual_part = f"#{qual}" if qual else ""
    return f"{req_id}#{tier_part}{rail_part}{qual_part}"
