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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from aterag.extract.models import TestCondition

DEFAULT_RULES_PATH = Path("config/scenario_rules.yaml")

#: 语义常量: 用于按"输出电流"语义筛选额定电流。kind 与单位名取自
#: condition_patterns.yaml 的封闭词表 —— 这里只做等值比较, 不引入新的词汇。
_KIND_OUTPUT_CURRENT = "output_current"
#: 输入侧的两个驱动 kind (见 _input_clause: 电压与频率都是输入侧的量)。
#: 取自 kinds.input 封闭词表, 不在代码里另造语义。
_KIND_INPUT_FREQUENCY = "input_frequency"
_KIND_LOAD = "load"
_KIND_INPUT_VOLTAGE = "input_voltage"
_KIND_DUTY = "duty"
#: 补齐层来源标记 (见 models.SRC_METHOD) —— 补进来的通用判据不作为命名依据。
_SRC_INDUSTRY_METHOD = "industry_method"
_UNIT_CURRENT = "A"

#: 语义常量: 限值的三个形态键(与 TestCondition.limits 的键同名)。
_LIMIT_KEYS = ("min", "typ", "max")

#: 限值子句的来源标记 (与 models.SRC_LIMITS 同值)。
_SRC_LIMITS = "limits"


def _limit_kind(cond: TestCondition) -> str:
    """限值列对应的语义分类 —— 判断"被测对象是什么量"的唯一依据。

    取自装配层写入的 ConditionClause.kind (source=limits), 不在此重新实现
    单位→kind 的映射: 那套映射由 condition_patterns.yaml 的 limit_kinds 与
    title_kinds 决定, 复用可保证命名层与限值子句的语义永远一致。实测 PA601 的
    SRC_LIMITS 子句 100% 落在封闭 kinds 词表内, 故该值可信。

    **两侧都要看**: 装配层按章节先验 (role) 决定限值子句归到哪一侧 ——
    输入特性表的条款 (role=input_domain) 限值落在**输入侧**, 输出侧只有补齐
    层加的通用判据。只看输出侧的话, 输入电压范围/输入频率这类条款取不到值,
    limit_kind 恒为空, 输入侧前缀会全部失效(比修之前更糟)。

    保护类条款的 kind 已被装配层显式覆盖成 protection_action (assembler 对
    ROLE_PROTECTION 有专门分支), 这里如实反映, 不做二次改判。

    取不到时返回空串 —— 命名层据此**不给**输入侧前缀。空值只出现在无数值的
    条款(信号类/功能要求类), 那些本就不该带输入前缀, 是安全默认。
    """
    for cl in (*cond.input_conditions, *cond.output_conditions):
        if cl.source == _SRC_LIMITS and cl.kind:
            return cl.kind
    return ""


#: 名称模板可用的渲染上下文 (键固定, 避免模板引用不存在变量)。
_NAME_CTX = frozenset({"value_g", "min_g", "max_g", "value", "rail", "unit"})
#: 工况维度标签模板可用的占位符。text = 规格书原文解析出的取值文本。
_DIM_LABEL_CTX = frozenset({"text", "key"})


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
class DimensionLabel:
    """把一个工况维度的取值渲染进场景名 (声明在 scenario_rules.yaml)。

    为什么必须有这一层: 场景名是产测人员唯一的辨识依据, 而同一条需求在不同
    工况档下会展开成多个场景 —— SR-1203 输出电流在 90~176Vac 下可拉到 7.401A,
    在 176~286Vac 下是 11.1A, 两者相差 50%。若名字里不带档位, 产测按名字
    执行必然有一个测点用错判据, 且不报错。

    key 是维度标识 (对应 dimensions[].key), 措辞与模板全在配置里 ——
    "输入{text}" 是产测行业对电压档的通用叫法, 属文档认知而非代码逻辑,
    焊进代码等于把这一套叫法固定在本实现。

    {text} 取 DimensionValue.text, 即规格书原文解析结果 ("90~176Vac")。
    代码不构造这个字符串, 也不在代码里出现任何档位数字。
    """

    id: str
    key: str
    pattern: str
    #: 只在该维度于本需求取到多个值时才渲染 (单值维度加了是噪声)
    only_when_multiple: bool = True

    def render(self, value: "DimensionValue") -> str:
        return self.pattern.format(text=value.text, key=value.key)


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
    #: 工况维度 -> 场景名标签 (见 DimensionLabel)。维度 key 不在代码里出现,
    #: 全部由配置声明; 未在此声明的维度不进名字 (如 load 走前缀机制)。
    dimension_labels: tuple[DimensionLabel, ...] = ()
    scenario_name: str = "{prefix}{subject}{rail_suffix}"
    rail_suffix_pattern: str = "@{rail}"
    #: 输入侧条目命中输入前缀时只输出前缀, 不拼接输出对象
    input_only_no_subject: bool = True
    #: 负载档命名要求单位为百分比 (见 config 注释: 速率类 load 不可当百分比)
    load_levels_require_unit_percent: bool = True
    #: 补完维度标签后仍重名时追加序号, 保证场景名全局唯一 (产测辨识依据)。
    #: 只保证唯一, 不解决语义重名 —— 语义重名靠 dimension_labels 治。
    dedupe_names: bool = True

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
            dimension_labels=tuple(
                DimensionLabel(
                    id=str(x.get("id", "")),
                    key=str(x.get("key", "")),
                    pattern=str(x.get("pattern", "")),
                    only_when_multiple=bool(x.get("only_when_multiple", True)),
                )
                for x in (n.get("dimension_labels") or [])
            ),
            scenario_name=str(n.get("scenario_name", "{prefix}{subject}{rail_suffix}")),
            rail_suffix_pattern=str(n.get("rail_suffix_pattern", "@{rail}")),
            input_only_no_subject=bool(n.get("input_only_no_subject", True)),
            load_levels_require_unit_percent=bool(n.get("load_levels_require_unit_percent", True)),
            dedupe_names=bool(n.get("dedupe_names", True)),
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
        seen_keys: dict[str, str] = {}
        for d in self.dimension_labels:
            if not d.id:
                bad.append("存在无 id 的 dimension_labels 条目")
            if not d.key:
                bad.append(f"naming.dimension_labels[{d.id}] 缺 key (维度标识)")
            elif d.key in seen_keys:
                # 同一维度配两个标签 -> 名字里会重复出现同一工况, 如
                # "输入90~176Vac输入90~176Vac"。必须当场拒绝。
                bad.append(
                    f"naming.dimension_labels 的 key={d.key} 重复声明 "
                    f"(已由 [{seen_keys[d.key]}] 覆盖)"
                )
            else:
                seen_keys[d.key] = d.id
            if not d.pattern:
                bad.append(f"naming.dimension_labels[{d.id}] 缺 pattern (不渲染等于没配)")
            for token in re.findall(r"\{([a-z_]+)\}", d.pattern):
                if token not in _DIM_LABEL_CTX:
                    bad.append(
                        f"naming.dimension_labels[{d.id}] 使用了未知占位符 {{{token}}} "
                        f"(可用: {sorted(_DIM_LABEL_CTX)})"
                    )
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
class DimensionMode:
    """维度内的一种取值形态。

    同一维度下两种形态并存 (如负载):
      level —— 静态档, 各取值**互斥**, 测20%就不测50%, 一个取值一个场景;
      slew  —— 变化序列, 有序路径, 必须整体执行 (25%->50%->25%), 一个序列一个场景。
    路径上的关键节点同时作为静态档记录 (derive_levels), 但那是从序列去重
    推导出来的, 不独立解析 —— 否则两条正则可能各认一半, 出现"路径里有但档位
    表里没有"的不一致。
    """

    id: str
    pattern: str = ""
    derive_levels: bool = False


@dataclass(frozen=True, slots=True)
class DimensionValue:
    """维度解析出的一个取值 (数值全部来自规格书原文)。"""

    key: str
    #: 呈现文本, 例 "90~176Vac" / "25%->50%->25%"
    text: str
    #: 关键节点/静态档, 从 text 去重推导 (仅 slew 形态产出)
    levels: tuple[str, ...] = ()
    #: 排序用数值; None 表示按声明顺序稳定排
    order: float | None = None
    #: 溯源: 命中的原文片段
    source_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "text": self.text,
            "levels": list(self.levels),
            "order": self.order,
            "source_text": self.source_text,
        }


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    """一个可展开的条件维度 (从规格书解析取值)。

    carrier 决定取值从哪来 —— 这是维度能否跨产品通用的关键:
      tier           —— 全局共享, 由 tiers 解析得到 (输入电压功率档);
      notes_span     —— 该条需求 notes 内的多档 (SR-1204 "90~176Vac: 400W;
                        176~286Vac: 600W"); 一条 notes 内取值互斥;
      repeated_row   —— 同 req_id 多行各带一个取值 (SR-1210 三行 20%/50%/最大);
                        每行自带其工况, 不靠 notes 再解析。
    """

    key: str
    label: str = ""
    unit_hint: str = ""
    pattern: str = ""
    from_notes: bool = True
    carrier: str = "notes_span"
    modes: tuple[DimensionMode, ...] = ()


@dataclass(frozen=True, slots=True)
class LoadDerivation:
    """轨级负载推导规则 (功率档封顶)。"""

    id: str
    basis: str
    formula: str
    tolerance: float = 1e-3
    regression_golden: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ScenarioRules:
    """场景规则集 (声明在 config/scenario_rules.yaml)。"""

    dimensions: tuple[DimensionSpec, ...] = ()
    tier_pattern: str = ""
    tier_from_notes: bool = True
    #: 档位来源条目的判定: 单位模式 + 标题模式。
    #: 不按标题**字面量**比对 —— 型号可能写"额定输出总功率"或英文
    #: "Total Output Power", 字面量一变档位解析就归零, 而推导随之静默失效
    #: (场景退回按额定电流判, 低压段击穿功率档)。
    tier_from_unit_pattern: str = ""
    tier_from_title_pattern: str = ""
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
                    carrier=str(d.get("carrier", "notes_span")),
                    modes=tuple(
                        DimensionMode(
                            id=str(m.get("id", "")),
                            pattern=str(m.get("pattern", "")),
                            derive_levels=bool(m.get("derive_levels", False)),
                        )
                        for m in (d.get("modes") or [])
                    ),
                )
                for d in (doc.get("dimensions") or [])
            ),
            tier_pattern=str(parse.get("pattern", "")),
            tier_from_notes=bool(parse.get("from_notes", True)),
            tier_from_unit_pattern=str(parse.get("from_unit_pattern", "")),
            tier_from_title_pattern=str(parse.get("from_title_pattern", "")),
            derivations=tuple(
                LoadDerivation(
                    id=str(x.get("id", "")),
                    basis=str(x.get("basis", "")),
                    formula=str(x.get("formula", "")),
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
            # carrier=tier 的维度取值来自 tiers 解析, 无需自带 pattern;
            # 其余 carrier 必须有取值来源, 否则该维度解析恒为空 —— 配置看着
            # 齐全, 展开时静默不生效, 是最难排查的一类"配置未生效"。
            if d.carrier != "tier" and not d.pattern and not d.modes:
                bad.append(
                    f"dimension[{d.key}] carrier={d.carrier} 既无 pattern 也无 modes "
                    "(取值无来源, 展开时恒为空)"
                )
            for m in d.modes:
                if m.pattern:
                    try:
                        re.compile(m.pattern)
                    except re.error as e:
                        bad.append(f"dimension[{d.key}].modes[{m.id}].pattern 正则非法: {e}")
        for t in self.tier_pattern and [self.tier_pattern] or []:
            try:
                re.compile(t)
            except re.error as e:
                bad.append(f"tiers.parse.pattern 正则非法: {e}")
        # 命名标签引用的维度必须已声明 —— 否则标签永远不会被渲染, 配置看着
        # 齐全但场景名里没有工况, 正是"同名不同判据"的成因。
        # 这里只查"未声明"; "未列入 dimension_order" 由 expand_scenarios 先报,
        # 以便那个更具体的配置错误不被本条盖住(既有契约: dimension_order 的
        # 报错必须优先于其它校验)。
        declared = {d.key for d in self.dimensions}
        for dl in self.naming.dimension_labels:
            if dl.key not in declared:
                bad.append(
                    f"naming.dimension_labels[{dl.id}] 引用了未声明的维度 {dl.key} "
                    f"(已声明: {sorted(declared)})"
                )
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
    """取输入侧的驱动子句 (无则 None)。

    认两种 kind: 输入电压与输入频率。二者都是"设定输入侧的量", 是输入侧命名
    前缀的语境。只认输入电压的话, SR-1103 交流输入频率取不到子句 -> 走不到
    input_levels 规则 -> 即使 limit_kind 与 limits 都对, 也只会退回标题兜底。
    """
    for kind in (_KIND_INPUT_VOLTAGE, _KIND_INPUT_FREQUENCY):
        for cl in cond.input_conditions:
            if cl.kind == kind:
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
    if "limit_kind_is" in w:
        # 限定"被测对象是什么量", 而不限"限值列有几个键"。
        # 只看键数时, 输出电压(min=3.45)、纹波(max=500mV)、上升时间(max=20ms)
        # 因为恰好只有 min 或 max 一个键, 会被输入前缀规则接走 —— 产测照
        # "输入最小值@-54V" 设输入电压, 测到的却不是被测对象。
        # limit_kind 来自 condition_patterns.yaml 的 limit_kinds (V→output_voltage
        # / mV→ripple / ms→timing), 单位缺省时回落 title_kinds, 与装配层同源。
        kinds = w["limit_kind_is"]
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if ctx.get("limit_kind") not in kinds:
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
        # 被测对象的量纲分类, 供 limit_kind_is 闸门使用 —— 见 _limit_kind
        "limit_kind": _limit_kind(cond),
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


def derive_scenario_name(
    cond: TestCondition,
    naming: NamingBook,
    rail: str = "",
    load_level: str = "",
    combo: Sequence[DimensionValue] = (),
    multi_counts: Mapping[str, int] | None = None,
) -> str:
    """场景名 = 条件名 + 轨后缀 + 工况标签 (同一条需求在不同工况下是不同的测点)。

    load_level 是本场景绑定的负载档, 取自维度解析结果。它必须参与命名, 而不
    只能靠 condition 的 load 子句: 有些行的负载档**只存在于 notes** 而没有
    对应的子句值 —— PA601 SR-1210 第三行写"最大输出负载测试"而典型值是 93,
    子句里没有百分比。命名若只看子句, 这一档就会退回前缀兜底, 被命名成
    "输入最小值"这种完全无关的名字, 产测人员看到后无从知道该测哪个负载点。

    只在子句命名**没有**给出负载前缀时补前缀, 否则会重复 —— "20%最大输出负载"
    这档子句已命名成"20%载效率", 再补一次就成了"20载20%载效率"。

    combo 是本场景绑定的全部维度取值。逐条按 naming.dimension_labels 渲染成
    标签附在轨后缀之后 —— 这是"同名场景靠名字即可分辨"的唯一依据: SR-1203
    输出电流在两个电压档下分别是 7.401A 和 11.1A, 判据差 50% 而名字必须不同。

    multi_counts 是各维度在本需求内的取值数。只有取值数 > 1 的维度才渲染标签
    (单值维度加上是噪声, 且会让本已可读的名字变长)。
    """
    base = derive_condition_name(cond, naming)
    label = _load_label(load_level)
    if label and not base.startswith(label):
        base = f"{label}{base}" if base else label
    if rail:
        base = f"{base}{naming.rail_suffix_pattern.format(rail=rail)}"
    tags: list[str] = []
    counts = multi_counts or {}
    for dl in naming.dimension_labels:
        for v in combo:
            if v.key != dl.key:
                continue
            if dl.only_when_multiple and counts.get(v.key, 1) < 2:
                continue
            tag = dl.render(v)
            if tag and tag not in tags:
                tags.append(tag)
    if tags:
        base = f"{base}[{'; '.join(tags)}]"
    return base


def _combo_load(combo: Sequence[DimensionValue]) -> str:
    """从维度取值组合里取出负载档文本 (供场景命名)。

    只认 load 维度, 且只取**静态档形态**: 变化序列(25%->50%->25%)不该折成一个
    百分比前缀 —— 那会让 200us 恢复时间的场景被命名成"50%载动态响应时间",
    而实际必须按 25→50→25 的路径执行。序列档另由 levels 提供。
    """
    for v in combo:
        if v.key == "load" and not v.levels:
            return v.text
    return ""


def _load_label(level_text: str) -> str:
    """负载档取值 -> 产测可读前缀 (数值全部来自规格书, 不臆造)。

    "20%最大输出负载" -> "20%载";  "50%负载" -> "50%载";
    "最大输出负载测试" -> "满载" (无数值时按满载表述, 与行业叫法一致)。
    """
    t = level_text.strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*%", t)
    if m:
        f = float(m.group(1))
        return f"{int(f) if f.is_integer() else f'{f:g}'}%载"
    if "最大" in t:
        return "满载"
    return ""


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
    #: 本场景适用的限值。多数场景等于需求的基准限值; 但同一指标在不同工况档下
    #: 可以有不同动作区间(规格书把替代区间写在备注里, 如 SR-1309
    #: "输入电压<176Vac, 过流点8.1A~18A"), 那一档必须用自己的值, 否则产测会
    #: 拿基准值(如 12A)去测低压段, 永远测不到该档的下边界。
    #: 与 derived 同理: 展开时算好挂在场景上, 下游不必再解一遍条件式。
    limits: dict[str, Any] = field(default_factory=dict)
    #: 命中替代限值时的原文依据(溯源)。空=用的是基准限值。
    limit_basis: str = ""
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
            "limits": self.limits,
            "limit_basis": self.limit_basis,
            "basis": self.basis,
            "source": self.source,
        }


#: 维度取值里的区间写法, 例 "90~176Vac" / "110.5~220V"
_RANGE_TEXT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*[~～]\s*(-?\d+(?:\.\d+)?)")
_GUARD_EPS = 1e-9


def _guard_holds(guard: Mapping[str, Any], bindings: Mapping[str, str]) -> bool:
    """备注里的条件式是否被本场景的维度取值覆盖。

    条件式是原文表述(如「输入电压<176Vac」), 维度取值是档位文本(如「90~176Vac」),
    两者来源不同, 只能在此处对上。判定规则: 该档位**整体**落在条件式描述的区间内
    才算命中 —— 条件式说的是"低于某阈值时用另一个限值", 若档位跨在阈值两侧就
    说不清该用哪个, 此时不命中(宁可用基准值, 也不猜)。

    取「整体落入」而非「边界相交」是有意的: 档位边界常与阈值重合(90~176 的上界
    正是 176), 边界相交会让两个相邻档同时命中, 基准值就永远用不上了。
    """
    dim = str(guard.get("dimension") or "")
    op = str(guard.get("op") or "")
    thr = guard.get("value")
    bound = bindings.get(dim) if dim else None
    if not dim or not bound or isinstance(thr, bool) or not isinstance(thr, (int, float)):
        return False
    m = _RANGE_TEXT_RE.search(bound)
    if not m:
        return False
    lo, hi = float(m.group(1)), float(m.group(2))
    if lo > hi:
        lo, hi = hi, lo
    t = float(thr)
    if op in {"<", "<=", "≤"}:
        return hi <= t + _GUARD_EPS
    if op in {">", ">=", "≥"}:
        return lo >= t - _GUARD_EPS
    return False


def resolve_scenario_limits(
    cond: TestCondition, bindings: Mapping[str, str]
) -> tuple[dict[str, Any], str]:
    """本场景适用的限值。返回 (limits, 原文依据); 依据为空表示用的是基准限值。

    基准限值来自表格的最小值/典型值/最大值列, 它对应默认工况档。备注里的
    「条件式 + 区间」是**分档替代值**: 命中就覆盖对应端点, 未命中的端点保留
    基准 —— 规格书常只改一端(如低压段只降下限 12A->8.1A, 上限 18A 两档相同)。
    """
    base = dict(cond.limits or {})
    out = dict(base)
    basis = ""
    for cl in (*cond.input_conditions, *cond.output_conditions):
        v = cl.value if isinstance(cl.value, Mapping) else None
        if not v:
            continue
        guard = v.get("guard")
        if not isinstance(guard, Mapping) or not _guard_holds(guard, bindings):
            continue
        for key, val in (("min", v.get("value")), ("max", v.get("value2"))):
            if val is not None:
                out[key] = val
        if v.get("unit"):
            out["unit"] = v["unit"]
        basis = str(guard.get("source_text") or "")
        break
    return out, basis


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


def _slew_label(nodes: Sequence[float], unit: str = "%") -> str:
    """变化序列的呈现文本, 例 "25%->50%->25%"。"""
    return "->".join(f"{n:g}{unit}" for n in nodes)


def parse_condition_dimensions(spec: DimensionSpec, c: TestCondition) -> list[DimensionValue]:
    """解析**单条需求**在某维度上的取值。

    工况维度必须逐需求解析, 不能做成全局取值池: 温度窗口只对 SR-1206/
    SR-1213 有意义, 负载档只对 SR-1210/1104 有意义。做成全局池后笛卡尔积会把
    无关工况绑到无关需求上(ESD 抗扰被标上"温度≤-30℃"), 场景数还会成倍膨胀 ——
    而产测看到的是一个自己根本不成立的工况。
    """
    return _parse_values(spec, (c,))


def parse_dimension_values(
    spec: DimensionSpec, conditions: Sequence[TestCondition]
) -> list[DimensionValue]:
    """从规格书原文解析某维度的取值 —— 代码只认正则, 数值全部来自原文。

    两种 notes 来源都要覆盖, 否则同一条需求的工况表达形式不同就解析不到:
      pattern + modes 都配  -> 先按 modes 切 (level/slew), 未命中的部分再按
                                顶层 pattern 兜底;
      只配 pattern          -> 整体按 pattern 切。
    """
    return _parse_values(spec, conditions)


def _parse_values(spec: DimensionSpec, conditions: Sequence[TestCondition]) -> list[DimensionValue]:
    if spec.carrier == "tier":
        return []  # tier 维度取值由 parse_tiers 提供, 这里不重复解析
    pats: list[tuple[DimensionMode, re.Pattern[str]]] = []
    for m in spec.modes:
        if m.pattern:
            try:
                pats.append((m, re.compile(m.pattern)))
            except re.error as e:
                raise ValueError(
                    f"dimension[{spec.key}].modes[{m.id}].pattern 正则非法: {e}"
                ) from e
    top = None
    if spec.pattern:
        try:
            top = re.compile(spec.pattern)
        except re.error as e:
            raise ValueError(f"dimension[{spec.key}].pattern 正则非法: {e}") from e

    out: list[DimensionValue] = []
    seen: set[tuple[str, str]] = set()
    for c in conditions:
        if not c.notes:
            continue
        if not spec.from_notes:
            continue
        for mode, rx in pats:
            for m in rx.finditer(c.notes):
                text = _clean_cell(m.group(0))
                if not text or (spec.key, text) in seen:
                    continue
                nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", m.group(0))]
                levels: tuple[str, ...] = ()
                order: float | None = None
                if mode.derive_levels:
                    # 路径上的关键节点同时作为静态档记录: 从序列去重, 不独立解析,
                    # 保证"路径里有"与"档位表里有"永远一致。
                    levels = tuple(dict.fromkeys(f"{n:g}%" for n in nums))
                elif mode.id == "level":
                    order = nums[0] if nums else None
                seen.add((spec.key, text))
                out.append(
                    DimensionValue(
                        key=spec.key,
                        text=_slew_label(nums) if mode.derive_levels else text,
                        levels=levels,
                        order=order,
                        source_text=text,
                    )
                )
        if top is not None:
            for m in top.finditer(c.notes):
                text = _clean_cell(m.group(0))
                if not text or (spec.key, text) in seen:
                    continue
                nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", m.group(0))]
                seen.add((spec.key, text))
                out.append(
                    DimensionValue(
                        key=spec.key,
                        text=text,
                        order=nums[0] if nums else None,
                        source_text=text,
                    )
                )
    # 数值维度升序, 无序值按声明顺序稳定排 —— case_code 由 seq 派生, 顺序不稳
    # 就会让幂等导入重复建用例。
    out.sort(key=lambda v: (v.order is None, v.order if v.order is not None else 0.0, v.text))
    return out


def _clean_cell(v: Any) -> str:
    """去掉 markdown 表格残留的加粗标记与首尾空白 (不改写原文语义)。"""
    return re.sub(r"\s+", " ", str(v or "").replace("**", "").replace("<br>", " ")).strip()


def parse_tiers(rules: ScenarioRules, conditions: Sequence[TestCondition]) -> list[Tier]:
    """从规格书原文解析输入电压档 —— 代码只认正则, 数字全部来自原文。

    档位是**跨需求共享**的: 它由 SR-1204(输出功率)的 notes 声明, 却约束
    SR-1203(输出电流) 等所有有轨需求的判据。故解析要看全部 conditions, 而不是
    调用方手里那一条 —— 只传单条需求必然解析不到档位, 轨级推导会静默失效。
    单需求查询场景须先全量解析再按 req_id 过滤, 不能就地调用本函数。
    """
    if not rules.tier_pattern:
        return []
    rx = re.compile(rules.tier_pattern)
    unit_rx = re.compile(rules.tier_from_unit_pattern) if rules.tier_from_unit_pattern else None
    title_rx = re.compile(rules.tier_from_title_pattern) if rules.tier_from_title_pattern else None
    out: list[Tier] = []
    seen: set[tuple[float, float, float]] = set()
    for c in conditions:
        # 档位声明在"整机总功率"那条需求上(PA601 SR-1204 输出功率, 无轨名)。
        # 判据用单位 W + 标题含"功率"两个模式, 任一命中即认定 —— 按标题
        # 字面量比对会在换写法时静默失配, 档位归零而推导随之失效。
        if unit_rx is not None:
            lim = c.limits or {}
            unit = str(lim.get("unit") or c.unit or "")
            if not (unit_rx.search(unit) or (title_rx is not None and title_rx.search(c.title))):
                continue
        elif title_rx is not None and not title_rx.search(c.title):
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

    判据只认 output_current 子句, 不看量纲 —— 见 _is_load_bearing 的说明:
    保护动作阈值(短路/过流)单位也是 A, 按量纲收会把"保护动作点"当成
    "该轨额定满载电流", 于是 -54V 轨的额定值被记成保护动作点(PA601 是
    18A 而非 11.1A), 轨级推导的封顶基准直接错一个量级。
    """
    out: dict[str, float] = {}
    for c in conditions:
        if not c.rail:
            continue
        mx = c.limits.get("max") if c.limits else None
        if mx is None:
            continue
        if not _has_output_current_kind(c):
            continue
        out.setdefault(c.rail, float(mx))
    return out


def _has_output_current_kind(c: TestCondition) -> bool:
    """该需求测的是否就是"这条轨能拉多大电流"。

    只认 output_current 子句 —— 这是"被测对象"的语义, 而非限值的量纲。
    limit_kinds 把 A 一律映射成 output_current, 但保护条款 (role=
    protection_response) 的限值子句在装配层已被改判成 protection_action
    (assembler 对 ROLE_PROTECTION 显式覆盖), 所以这里看到的是保护语义。

    漏掉这道区分的具体后果: PA601 SR-1309 输出过流保护动作区间 12~18A,
    派生电流却被挂成 7.401A —— 落在动作区间之下, 产测按它设负载根本不触发
    保护, 保护功能测不出来却显示通过。
    """
    return any(cl.kind == _KIND_OUTPUT_CURRENT for cl in c.output_conditions)


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


def _is_load_bearing(c: TestCondition) -> bool:
    """该需求的判据是否真的是"这一轨能拉多大电流"。

    轨级推导出来的电流只对输出电流类需求有意义。绑到额定输出电压(SR-1200)、
    温度系数(SR-1217)、短路保护(SR-1308)上是错的: 那些判据是电压/温度/保护点,
    挂一个输出电流只会让产测人员以为还要额外拉这个电流 —— 或是更糟, 拿它去
    反推负载设定, 于是给电压判据的用例也去按 7.401A 设负载。

    判据只认 output_current 子句, 不看量纲 (见 _has_output_current_kind):
    短路/过流保护的单位也是 A, 按量纲判会把它们一起收进来, 而保护动作点与
    输出电流上限是两种量 —— 前者是"电流多大时保护动作", 后者是"这轨能拉多大"。
    PA601 SR-1309 动作区间 12~18A, 挂上 7.401A 后产测设负载根本不触发保护。

    与 _rated_currents 同源(同一个 _has_output_current_kind), 两处必须一致 ——
    否则会出现"额定表里有这条轨, 但它不产推导场景"的空洞。
    """
    if not c.rail:
        return False
    return _has_output_current_kind(c)


def _derive_load(
    rules: ScenarioRules,
    tiers: Sequence[Tier],
    rated_a: Mapping[str, float],
    volts: Mapping[str, float],
) -> dict[tuple[float, float, float], dict[str, float]]:
    """按功率档推导各轨满载电流。

    返回 {(min,max,power) -> {rail: current}}。

    轨序按各轨额定功耗 (U*I_rated) 升序自动排定, 语义是"先满足的轨先分配":
      * 靠前的轨按其额定电流取值, 功耗计入 P_aux(它要先吃掉一部分功率);
      * 最后一轨 (额定功耗最大者, 即主轨) 吃剩余功率 (P - P_aux) / U。
    这样 400W 档下 3.45V 轨 (0.345W) 先取 0.1A, 主轨 (599.4W) 得
    (400-0.345)/54=7.401A, 与需求方已确认的口径一致; 顺序反了会算出
    400/54=7.407A 而偏 6mA。排序依据是功耗大小而非轨名, 故换型号自动成立。
    """
    out: dict[tuple[float, float, float], dict[str, float]] = {}
    der = next((d for d in rules.derivations if d.formula), None)
    if der is None:
        return out
    # 末轨吃剩余功率, 故轨序必须让"额定功耗最大者"排在末位 (主轨)。
    # 排序依据额定功耗 U*I_rated —— 这是数据驱动、与产品无关的语义:
    # 哪条轨在满载时吃掉的功率最多, 它就是受功率档封顶的那条。
    #
    # 早先靠配置里的 priority_rails (PA601 的 ["3.45V","-54V"]) 声明, 换轨名后
    # 该列表整体失配 -> 落进 sorted() 按字母序兜底 -> 主轨/辅轨可能颠倒 ->
    # 算出的电流符号都可能是负的(实测 -48V/12V 型号得 12V=-46.667A), 而全程
    # 不报错。字母序与功耗毫无关系, 拿它兜底等于随机选主轨。
    order = sorted(
        (r for r in rated_a if r in volts),
        key=lambda r: (rated_a[r] * volts[r], r),
    )
    if not order:
        # fail-closed: 推导要按轨分配功率, 却没有可用轨时不能静默返回空 ——
        # 空结果会让调用方以为"该档无需推导", 于是场景直接拿额定电流当判据。
        # 额定 11.1A 只在 >=176Vac 成立, 按它设低压段负载会击穿 400W 功率档。
        raise ValueError(
            "轨级负载推导无可用轨 (rated/voltage 均缺失), 无法确定吃剩余功率的轨; "
            "检查规格书是否给出带轨名的输出电流行(须同时有输出电流语义与轨电压): "
            f"available_rated={sorted(rated_a)} available_volts={sorted(volts)}"
        )
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
    # 配置自洽性先于数据校验: dimension_order 引用未声明维度是配置错误, 必须
    # 优先报出 —— 否则它会被后续的"数据不足"类报错盖住, 让人误以为是数据问题。
    unknown = [k for k in rules.dimension_order if k not in {d.key for d in rules.dimensions}]
    if unknown:
        raise ValueError(
            f"ordering.dimension_order 引用了未声明的维度: {unknown} — "
            f"已声明: {[d.key for d in rules.dimensions]}"
        )
    tiers = parse_tiers(rules, conditions)
    rated = _rated_currents(conditions)
    volts = _rail_voltages(conditions)
    derived = _derive_load(rules, tiers, rated, volts) if tiers else {}

    # tier 维度的取值来自已解析的档位, 全局共享(功率档对所有需求一致);
    # 其余工况维度逐需求解析 —— 见 parse_condition_dimensions 的说明。
    tier_pool: list[DimensionValue] = [
        DimensionValue(
            key=spec.key,
            text=f"{t.min_vac:g}~{t.max_vac:g}Vac",
            order=t.min_vac,
            source_text=t.source_text,
        )
        for spec in rules.dimensions
        if spec.carrier == "tier"
        for t in tiers
    ]
    specs_by_key = {d.key: d for d in rules.dimensions}
    active_keys = [k for k in rules.dimension_order if k in specs_by_key]

    res = ScenarioResult(tiers=tiers)
    # seq 必须"每需求内唯一且连续": P2 的 case_code = f"{req_id}-S{seq:03d}",
    # 同一需求的多个来源行(多轨/多工作制)共享 req_id, 若每个行各自从 0 开始,
    # case_code 就会撞车 -> 幂等导入把多行并成一条, 条件集静默丢失。
    # 故序号按需求维度统一分配, 而不是按行。
    seq_by_req: dict[str, int] = {}
    for c in conditions:
        # 本条需求的维度取值池: tier 维度全局共享, 其余逐需求解析。
        # 只有实际取到值的维度才进笛卡尔积 —— 空池进积等于 0 个场景。
        # 有轨的需求才需要功率档绑定: 无轨行是整机要求(如 SR-1204 输出功率),
        # 把档位绑上去会让同一条整机要求被当成"逐档的逐轨判据"。
        # 有轨但 notes 为空的行(如 SR-1203 "长期工作")**必须**绑档位 ——
        # 它就是额定 11.1A 那条, 不绑档就会退回按额定值判, 低压段击穿 400W。
        pools: list[list[DimensionValue]] = []
        for k in active_keys:
            spec = specs_by_key[k]
            if spec.carrier == "tier":
                vals = tier_pool if c.rail else []
            else:
                vals = parse_condition_dimensions(spec, c)
            if vals:
                pools.append(vals)
        # 各维度在本需求内的取值数 —— 命名只给"取到多个值"的维度加标签。
        # 单值维度(如某需求只有一个温度档)加标签是噪声。
        multi_counts = {pool[0].key: len(pool) for pool in pools if pool}
        # 无任何维度取值可用时, 每条需求一个基线场景(不拆)
        if not pools:
            # 无维度取值 -> 无分档条件式可判, 限值取基准值
            scen_limits, scen_basis = resolve_scenario_limits(c, {})
            res.scenarios.append(
                Scenario(
                    # 必须含轨与行限定词: 同一编号可能有多行(如 SR-1100 标称输入
                    # 分 110Vac/220Vac 两行共用 req_id), 只用 req_id#base 会让它们
                    # 撞成同一个 id, 幂等导入把两行并成一条用例, 条件集静默少一半。
                    scenario_id=_sid(c.req_id, (), c.rail, c.notes),
                    # seq 按需求递增, 不能恒为 0: 同编号多行(如 SR-1100 分
                    # 110Vac/220Vac 两行)会撞 (req_id, seq), case_code 随之撞车。
                    seq=seq_by_req.get(c.req_id, 0),
                    req_id=c.req_id,
                    title=c.title,
                    rail=c.rail,
                    name=derive_scenario_name(c, rules.naming, c.rail),
                    limits=scen_limits,
                    limit_basis=scen_basis,
                    basis="no_dimension_split",
                    source="spec",
                )
            )
            seq_by_req[c.req_id] = seq_by_req.get(c.req_id, 0) + 1
            continue
        seq = seq_by_req.get(c.req_id, 0)
        # 笛卡尔积: 维度按 dimension_order 声明顺序, 越靠外的维度变化越慢,
        # 保证同一 (需求, 轨) 下 seq 递增顺序可复现。
        for combo in _combos(pools):
            binding = {v.key: v.text for v in combo}
            # 分档限值: 同一指标的判据可能随工况档变化(备注里的"条件式 + 区间"),
            # 必须按本组合的绑定解析, 否则各档共用基准值。
            scen_limits, scen_basis = resolve_scenario_limits(c, binding)
            # tier 取值决定功率档与轨级推导: 只认 carrier=tier 维度给出的档位,
            # 其余维度只绑定工况, 不影响功率分配。
            tier = _tier_of(combo, tiers)
            # 轨级电流只对输出电流类需求成立 —— 见 _is_load_bearing。
            # 其余有轨需求仍要绑档位(档位是通用工况), 只是不挂推导电流。
            d = (
                derived.get((tier.min_vac, tier.max_vac, tier.power_w), {})
                if tier and _is_load_bearing(c)
                else {}
            )
            for rail in c.rail and [c.rail] or []:
                if not d:
                    # 档位已绑、但不产推导电流 -> 基线场景(判据来自规格书本身)。
                    res.scenarios.append(
                        Scenario(
                            scenario_id=_sid(c.req_id, combo, rail, c.notes),
                            seq=seq,
                            req_id=c.req_id,
                            title=c.title,
                            rail=rail,
                            bindings=binding,
                            limits=scen_limits,
                            limit_basis=scen_basis,
                            name=derive_scenario_name(
                                c, rules.naming, rail, _combo_load(combo), combo, multi_counts
                            ),
                            basis=(f"tier_bound:{tier.power_w:g}W" if tier else "dimension_split"),
                            source="spec",
                        )
                    )
                    seq += 1
                    continue
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
                            scenario_id=_sid(c.req_id, combo, rail, c.notes),
                            seq=seq,
                            req_id=c.req_id,
                            title=c.title,
                            rail=rail,
                            bindings=binding,
                            limits=scen_limits,
                            limit_basis=scen_basis,
                            derived={rail: d[rail]},
                            name=derive_scenario_name(
                                c, rules.naming, rail, _combo_load(combo), combo, multi_counts
                            ),
                            basis=(
                                f"tier_power_capped:{tier.power_w:g}W"
                                if tier
                                else "dimension_split"
                            ),
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
                        scenario_id=_sid(c.req_id, combo, "", c.notes),
                        seq=seq,
                        req_id=c.req_id,
                        title=c.title,
                        rail="",
                        bindings=binding,
                        limits=scen_limits,
                        limit_basis=scen_basis,
                        name=derive_scenario_name(
                            c, rules.naming, "", _combo_load(combo), combo, multi_counts
                        ),
                        basis=(
                            f"tier_power_capped:{tier.power_w:g}W" if tier else "dimension_split"
                        ),
                        source="spec",
                    )
                )
                seq += 1
        # 该需求的下一行从当前序号继续, 保证 (req_id, seq) 全局唯一
        seq_by_req[c.req_id] = seq
    _assert_unique(res.scenarios)
    if rules.naming.dedupe_names:
        _dedupe_names(res.scenarios)
    return res


def _dedupe_names(scenarios: list[Scenario]) -> None:
    """场景名全局唯一 —— 产测人员靠名字分辨测点, 重名等于让人瞎测。

    只保证唯一, 不解决语义重名: 补齐顺序与 scenario_id 一致(按需求与序号),
    所以同一个名字的第 2 个场景拿 "_2" 时, 其 (req_id, seq) 不变 —— 用例编号
    稳定, 只是名字带了区分后缀。

    真正的语义重名 (如 18 条不同需求都叫"信号状态") 只能靠
    naming.dimension_labels 与被测对象名去治; 这里只是最后一道闸, 保证
    "按名字执行"这件事不会因为重名而指向错误判据。
    """
    if not scenarios:
        return
    seen: dict[str, int] = {}
    # 稳定化: 覆盖写 dataclass(frozen) 需重建, 按 (req_id, seq) 顺序处理
    ordered = sorted(scenarios, key=lambda s: (s.req_id, s.seq))
    repl: dict[int, str] = {}
    for s in ordered:
        n = seen.get(s.name, 0) + 1
        seen[s.name] = n
        if n == 1:
            continue
        repl[id(s)] = f"{s.name}_{n}"
    if not repl:
        return
    for i, s in enumerate(scenarios):
        new = repl.get(id(s))
        if new is not None:
            scenarios[i] = replace(s, name=new)


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


def _combos(pools: Sequence[Sequence[DimensionValue]]) -> list[tuple[DimensionValue, ...]]:
    """维度取值的笛卡尔积 (最外层 = 最先声明的维度)。"""
    out: list[tuple[DimensionValue, ...]] = [()]
    for pool in pools:
        out = [(*base, v) for base in out for v in pool]
    return out


def _tier_of(combo: Sequence[DimensionValue], tiers: Sequence[Tier]) -> Tier | None:
    """从维度取值组合里还原对应的功率档。

    按**取值文本**匹配, 不能拿维度 key 去查以档位文本为键的字典 —— key 与
    档位文本本就不同源, 那样查永远落空, 推导会静默变成空字典(所有有轨场景
    被判"该轨不由档位供电"而整体排除, 场景数从数百掉到几十)。

    同一组合内可能有多维, 谁先命中以谁为准: carrier=tier 的维度已在构造取值池
    时保证文本规范, 而工况维度取值不会与档位文本同形。
    """
    by_text = {f"{t.min_vac:g}~{t.max_vac:g}Vac": t for t in tiers}
    for v in combo:
        t = by_text.get(v.text)
        if t is not None:
            return t
    return None


def _sid(req_id: str, combo: Sequence[DimensionValue], rail: str, notes: str = "") -> str:
    """场景标识。

    必须含 req_id + 全部维度取值 + 轨 + 行限定词:
      * 同一需求可能有多行(多轨拆分、以及同轨不同工作制, 如 SR-1203 有
        -54V 长期/-54V 短期/3.45V 长期三行共用一个 req_id);
      * 维度取值必须全部进标识 —— 否则两个工况不同的场景(效率 20% 档 vs 50% 档)
        会撞成同一个 id, 幂等导入把两行并成一条用例, 判据只剩一个。
      * P2 的 case_code 由本标识派生, 一旦重复, 幂等导入就会把多行合并成
        一条用例, 条件集静默丢失 —— 这是最难在产线上发现的一类错误。
    notes 在此只作区分限定词(内部空白归一), 不改写原文 —— 原文仍由
    TestCondition.notes 原样保留。
    """
    dim_part = "".join(f".{_slug(v.key)}{_slug(v.text)}" for v in combo)
    rail_part = f"@{rail}" if rail else "@na"
    qual = re.sub(r"\s+", "", notes or "")[:12]
    qual_part = f"#{qual}" if qual else ""
    return f"{req_id}#{dim_part}{rail_part}{qual_part}"


def _slug(s: str) -> str:
    """维度键/取值压成标识片段: 保留可读字符, 其余一律去噪。

    取值里可能有 "~" "-" ">" "℃" 等符号, 直接进标识虽合法但可读性差,
    且符号差异可能造成本应不同的两个 id 撞车(挤掉分隔符后)。统一转成
    字母数字并把其余字符编码为两位十六进制, 保证映射是单射。
    """
    out: list[str] = []
    for ch in str(s):
        if ch.isalnum() or ch in "_":
            out.append(ch)
        else:
            out.append(f"~{ord(ch):02x}")
    return "".join(out)
