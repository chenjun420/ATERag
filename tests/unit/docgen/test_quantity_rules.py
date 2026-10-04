"""``docgen.quantity_rules`` 的单元测试。

两条主线:

1. **规则表本身的性质** —— 优先级、例外在前、不覆盖 U.5、死规则可检出。
2. **规则与方案自己写的量纲对照** (:class:`TestRulesAgreeWithDeclaredDimensions`) ——
   这是唯一能证伪规则表的东西。语料里有 114 条公式自带量纲列, 那是方案自己
   给出的答案; 规则推出的量纲与它矛盾, 就是规则错了。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aterag.docgen.expr_norm import normalize_equation
from aterag.docgen.quantity_rules import (
    RULES,
    QuantityDictionary,
    build_dictionary,
    unruleable_stems,
)
from aterag.docgen.spec_parse import parse_formula_rows, read_spec
from aterag.docgen.symbols import parse_spec_dimension, parse_symbol_table
from aterag.solver.symbolic import DIMENSIONLESS

SPEC = Path(__file__).resolve().parents[3] / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md"

U5 = """\
### U.5 全局符号表（命名空间隔离）

| 符号 | 含义 | 量纲 | 命名空间 |
|---|---|---|---|
| `V_in`, `V_out` | 电压 | `[V]` | `E` |
| `f_sw` | 开关频率 | `[Hz]` | `J` |
| `R_θjc` | 热阻 | `[K/W]` | `N` |
| `k`(下垂系数) | 下垂斜率 | `[Ω]` | `Q` |
| `U`, `u_c`, `u_A`, `u_B`, `k` | 不确定度、包含因子 | `[X]`,`[1]` | `M` |
| `DC`, `PFH`, `PFD` | 功能安全指标 | 见各条 | `L5` |
"""


@pytest.fixture(scope="module")
def dictionary() -> QuantityDictionary:
    return build_dictionary(parse_symbol_table(U5.splitlines())[0])


def dim(**kw: float):  # type: ignore[no-untyped-def]
    from aterag.solver.symbolic import DIMENSION_COMPONENTS

    base = dict.fromkeys(DIMENSION_COMPONENTS, 0.0)
    base.update(kw)
    return base


class TestPriority:
    def test_u5_namespace_match_beats_rules(self, dictionary: QuantityDictionary) -> None:
        """U.5 是方案自己声明的, 必须压过任何命名推断。"""
        res = dictionary.resolve("V_in", "E")
        assert res.source == "u5-namespace"
        assert res.dimension == parse_spec_dimension("[V]")

    def test_u5_unique_used_when_namespace_misses(self, dictionary: QuantityDictionary) -> None:
        """``V_in`` 标着命名空间 E, 但 J 命名空间的公式也在用它。

        U.5 全库只有一条 ``V_in`` 条目时不存在歧义, 可以用 —— 这是**唯一**
        允许的跨命名空间回退, 且必须是「全库唯一」, 不是前缀相同。
        """
        res = dictionary.resolve("V_in", "J")
        assert res.source == "u5-unique"
        assert res.dimension == parse_spec_dimension("[V]")

    def test_no_prefix_fallback_between_namespaces(self, dictionary: QuantityDictionary) -> None:
        """``k`` 在 Q 是 [Ω]、在 M 是 [1] —— 不得在两者之间猜。"""
        assert dictionary.resolve("k", "Q").dimension == parse_spec_dimension("[Ω]")
        assert dictionary.resolve("k", "M").dimension is None

    def test_rule_only_fires_when_u5_silent(self, dictionary: QuantityDictionary) -> None:
        res = dictionary.resolve("V_rms", "E")
        assert res.source == "rule:voltage"

    def test_every_resolution_carries_a_source(self, dictionary: QuantityDictionary) -> None:
        """没有任何变量允许「悄悄」拿到量纲。"""
        for sym in ("V_in", "V_rms", "k", "n", "__不存在__"):
            assert dictionary.resolve(sym, "E").source or dictionary.resolve(
                sym, "E"
            ).reason


class TestDeferralDoesNotBlockRules:
    def test_u5_says_see_elsewhere_but_rule_can_supply(self, dictionary: QuantityDictionary) -> None:
        """「见各条」= 方案没写, 不是方案禁止。

        早先一版在这里直接返回未解析, reliability-rate 规则因此**一次都没命中**
        (死规则), 而 PFH/PFD 正是靠它才能参与齐次性判定。
        """
        res = dictionary.resolve("PFH", "L5")
        assert res.dimension == parse_spec_dimension("[T⁻¹]")
        assert res.source == "rule:reliability-rate"

    def test_source_makes_it_visible_that_u5_did_not_say_it(self, dictionary: QuantityDictionary) -> None:
        """来源必须标成 rule:, 审计才看得出这不是 U.5 说的。"""
        assert not dictionary.resolve("PFH", "L5").source.startswith("u5")


class TestExceptionOrdering:
    """例外必须排在通例前面 —— 顺序即优先级。"""

    def test_thermal_resistance_beats_resistance(self, dictionary: QuantityDictionary) -> None:
        assert dictionary.resolve("R_θjc", "N").dimension == parse_spec_dimension("[K/W]")
        assert dictionary.resolve("R_th", "N").source == "rule:thermal-resistance"

    def test_plain_resistance_still_ohm(self, dictionary: QuantityDictionary) -> None:
        res = dictionary.resolve("R_load", "E")
        assert res.dimension == parse_spec_dimension("[Ω]")
        assert res.source == "rule:resistance"

    def test_thermal_capacitance_beats_capacitance(self, dictionary: QuantityDictionary) -> None:
        assert dictionary.resolve("C_th", "N").dimension == parse_spec_dimension("[J/K]")

    def test_capacitance_thermal_rule_precedes_capacitance_rule(self) -> None:
        order = [r.rule_id for r in RULES]
        assert order.index("capacitance-thermal") < order.index("capacitance")
        assert order.index("thermal-resistance") < order.index("resistance")

    def test_rule_ids_are_unique(self) -> None:
        ids = [r.rule_id for r in RULES]
        assert len(ids) == len(set(ids))


class TestDeliberateNonCoverage:
    """刻意不给规则的词干, 原因必须能回答。"""

    @pytest.mark.parametrize("stem", ["T", "A", "H", "U", "u", "s", "z", "y", "e", "k", "x"])
    def test_ambiguous_stems_have_no_rule(self, stem: str) -> None:
        """存在真实歧义的词干不得有规则 —— 给了就是伪造量纲。"""
        for rule in RULES:
            assert not re.search(rule.pattern, f"{stem}_probe"), rule.rule_id

    def test_reason_is_specific_not_generic(self, dictionary: QuantityDictionary) -> None:
        res = dictionary.resolve("T_s", "J")
        assert res.dimension is None
        assert "T" in (res.reason or "") and "温度" in (res.reason or "")

    def test_unruleable_stems_are_documented(self) -> None:
        stems = unruleable_stems()
        assert "T" in stems and "A" in stems
        for stem, why in stems.items():
            assert why.strip(), stem


class TestRuleTableHygiene:
    def test_every_rule_has_a_rationale(self) -> None:
        """没有理由的规则不允许进表 —— 复核的人要知道依据, 不只看结果。"""
        for rule in RULES:
            assert len(rule.rationale.strip()) >= 10, rule.rule_id

    def test_rule_ids_are_descriptive(self) -> None:
        for rule in RULES:
            assert re.fullmatch(r"[a-z0-9-]+", rule.rule_id), rule.rule_id


@pytest.mark.slow
class TestRulesAgreeWithDeclaredDimensions:
    """唯一能证伪规则表的东西: 与方案自己写的量纲列对照。"""

    @staticmethod
    def declared_dimension(text: str):  # type: ignore[no-untyped-def]
        """解析方案量纲列, 含复合记号 ``[F]·[V²]·[Hz] = [W]``。

        复合记号带 ``=`` 时取**等号右侧** —— 那是方案给出的最终答案, 左侧是
        推导过程。取左侧会把 ``[F]·[V²]·[Hz]`` 与 ``[W]`` 一起塞进解析器, 得到
        一个把等号也当记号的结果, 于是正确��规则会被误判成不一致。
        """
        body = text.strip()
        if "=" in body:
            body = body.rsplit("=", 1)[1]
        # 量纲列里一行可能给**多个符号**各写一个量纲 (``[Ω]`,`[Hz]``)。那种情况
        # 无法确定这一行声明的是哪个符号的量纲, 必须跳过 —— 强行取第一个会把
        # 规则的正确结果判成冲突。
        if body.count("[") > 1:
            return None
        return parse_spec_dimension(body)

    def test_declared_dimensions_are_reproduced(self) -> None:
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        lines = read_spec(SPEC)
        rep = parse_formula_rows(lines)
        dictionary = build_dictionary(parse_symbol_table(lines)[0])

        checked = 0
        mismatches: list[tuple[str, str, str]] = []
        for row in rep.rows:
            if not row.dimension_text or not row.expression:
                continue
            want = self.declared_dimension(row.dimension_text)
            if want is None:
                continue
            norm = normalize_equation(row.expression)
            if not norm.ok or not norm.lhs or norm.lhs not in norm.variables:
                continue
            domain = re.match(r"F_([A-Z])(?=[._])", row.formula_id)
            ns = domain.group(1) if domain else None
            got = dictionary.resolve(norm.lhs, ns).dimension
            if got is None:
                continue
            checked += 1
            if got != want:
                mismatches.append((row.formula_id, row.dimension_text, str(got)))

        assert checked >= 20, f"只有 {checked} 条可比对, 对照没有说服力"
        assert not mismatches, f"规则与方案自带量纲冲突: {mismatches}"


@pytest.mark.slow
def test_no_dead_rules_in_corpus() -> None:
    """一条规则在真实语料里一次都命中不到, 要么删掉要么说明理由。

    ``reliability-rate`` 就这样被揪出来过: U.5 写「见各条」时提前返回, 规则
    永远轮不到执行。
    """
    if not SPEC.is_file():
        pytest.skip(f"方案文件不在预期位置: {SPEC}")
    lines = read_spec(SPEC)
    rep = parse_formula_rows(lines)
    dictionary = build_dictionary(parse_symbol_table(lines)[0])

    fired: set[str] = set()
    for row in rep.rows:
        norm = normalize_equation(row.expression)
        if not norm.ok:
            continue
        domain = re.match(r"F_([A-Z])(?=[._])", row.formula_id)
        ns = domain.group(1) if domain else None
        for var in norm.variables:
            res = dictionary.resolve(var, ns)
            if res.is_rule_derived:
                fired.add(res.source.split(":", 1)[1])

    dead = [r.rule_id for r in RULES if r.rule_id not in fired]
    assert not dead, f"死规则 (语料里一次都没命中): {dead}"


def test_dimensionless_rule() -> None:
    """无量纲规则也要能表达 —— 效率、占空比这类。"""
    assert parse_spec_dimension("无量纲") == DIMENSIONLESS
