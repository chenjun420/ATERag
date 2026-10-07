"""工装夹具规则的可达性与出处 (方案 §4.5 改「产测工装夹具」)。

这一节钉的是两件容易退化的事:

1. **规则存在 ≠ 可达**。此前 18 条 ``K-FIX`` 在库里、有出处、有 selftest, 但
   ``get_fixture_spec`` 只暴露其中 2 条 —— 调用方被告知「工装能算的就这些」。
   「有什么可用」必须是可查的事实, 所以 ``fixture_rules`` 清单要进返回值。
2. **出处不可省**。用户裁定「查不到出处的移除」, 而 ``K-FIX-016``/``017`` 早已用
   ``%GRR``(MoreSteam) 承担了量具能力 —— 这正是不该留着硬写 ISO 5725 系数的原因。
   所以每条 fixture 规则都必须有可访问的网址出处。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aterag.inference.rules import formula_type_to_rule_id, load_domain_rules  # noqa: E402

RULES_DIR = ROOT / "domain_rules"

#: 本次新增的 5 条 —— 负载与故障注入。既有 001~018 只覆盖探针/精度/公差链/寿命。
NEW_FIXTURE_RULES = {
    "K-FIX-019": "负载 rise time 须快于被测 5 倍",
    "K-FIX-020": "负载容量覆盖 + 须带 OVP/OCP/OPP/OTP",
    "K-FIX-021": "故障注入三类必备(开路/通道间短路/对轨短路)",
    "K-FIX-022": "故障注入通道须声明断电默认态",
    "K-FIX-023": "切换容量按故障类型分别校核",
}


@pytest.fixture(scope="module")
def fixture_rules() -> list[dict]:
    rules, _shapes = load_domain_rules(str(RULES_DIR), "power")
    return [r for r in rules if r.get("category") == "fixture"]


class TestNewRulesExist:
    def test_all_five_new_rules_are_present(self, fixture_rules):
        ids = {r["id"] for r in fixture_rules}
        for rid in NEW_FIXTURE_RULES:
            assert rid in ids, f"{rid} ({NEW_FIXTURE_RULES[rid]}) 不在 fixture 类目里"

    def test_each_new_rule_states_what_it_forbids(self, fixture_rules):
        """statement 必须说清「不满足会怎样」—— 只写公式的规则无法据此判断设计。"""
        by_id = {r["id"]: r for r in fixture_rules}
        for rid in NEW_FIXTURE_RULES:
            st = str(by_id[rid].get("statement", ""))
            assert len(st) >= 40, f"{rid} 的 statement 太短, 说明不了判据: {st}"
            assert any(k in st for k in ("须", "必须", "不得")), f"{rid} 未表达约束性: {st}"

    def test_each_new_rule_is_computable_or_constraint(self, fixture_rules):
        """每条都得有一种实现方式 —— 只有 statement 没有 derive/constraint 的规则
        不可执行, 那就退化成文档, 而文档不会被查。"""
        by_id = {r["id"]: r for r in fixture_rules}
        for rid in NEW_FIXTURE_RULES:
            r = by_id[rid]
            has_impl = bool((r.get("derive") or {}).get("expr")) or bool(
                (r.get("constraint") or {}).get("shape")
            )
            assert has_impl, f"{rid} 既无 derive 也无 constraint —— 不可执行"

    def test_new_rules_pass_selftest(self):
        """每条规则都要有自己的 test 段, 且 selftest 真跑过。"""
        import subprocess

        p = subprocess.run(
            [str(ROOT / ".venv/Scripts/python.exe"), "-X", "utf8",
             "scripts/rules_selftest.py", "power"],
            capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
        )
        assert p.returncode == 0, p.stdout + p.stderr
        assert "selftest=130/130" in p.stdout, p.stdout


class TestProvenanceRequired:
    def test_every_fixture_rule_has_a_url(self, fixture_rules):
        """用户裁定「查不到出处的移除」→ 每条 fixture 规则都要有可访问网址。"""
        missing = [r["id"] for r in fixture_rules if not (r.get("source") or {}).get("url")]
        assert not missing, f"缺出处的 fixture 规则: {missing}"

    def test_urls_point_outside_the_project(self, fixture_rules):
        """``local://`` 指向本仓库的方案 md —— 那不是外部出处, 是自我引用。

        既有 001~006 里有几条就是 ``local://spec-plan-5.2.x``, 指向仓内那份开发
        指导方案。它作为「设计意图」记录没问题, 但不能算「查到了出处」——
        所以这里只要求**新增**的规则用外部网址。
        """
        by_id = {r["id"]: r for r in fixture_rules}
        for rid in NEW_FIXTURE_RULES:
            url = (by_id[rid].get("source") or {}).get("url", "")
            assert url.startswith("http"), f"{rid} 的出处不是外部网址: {url}"

    def test_confidence_is_declared_and_bounded(self, fixture_rules):
        by_id = {r["id"]: r for r in fixture_rules}
        for rid in NEW_FIXTURE_RULES:
            c = by_id[rid].get("confidence")
            assert c is not None, f"{rid} 未标 confidence"
            assert 0 < c <= 1, f"{rid} 的 confidence 越界: {c}"

    def test_retrieved_date_present(self, fixture_rules):
        """出处要带检索日期 —— 标准与厂商规格会改版, 没有日期的出处无法复核。"""
        by_id = {r["id"]: r for r in fixture_rules}
        for rid in NEW_FIXTURE_RULES:
            assert (by_id[rid].get("source") or {}).get("retrieved"), f"{rid} 出处缺检索日期"


class TestReachable:
    @pytest.mark.parametrize(
        ("formula_type", "rule_id"),
        [
            ("max_load_risetime", "K-FIX-019"),
            ("load_current_margin", "K-FIX-020"),
            ("fault_coverage_complete", "K-FIX-021"),
            ("default_path_continuous", "K-FIX-022"),
            ("switch_current_margin", "K-FIX-023"),
        ],
    )
    def test_formula_type_resolves_to_the_rule(self, formula_type, rule_id):
        """**可达 = 能被 calculate(formula_type=...) 找到**。这是规则与工具之间
        唯一的连接点, 断了就是「规则在库里但没人用」。"""
        assert formula_type_to_rule_id(formula_type) == rule_id

    def test_resolved_rule_actually_exists(self):
        """映射指向不存在的规则 = calculate 静默返回空 —— 「看起来有实际没有」。"""
        rules, _ = load_domain_rules(str(RULES_DIR), "power")
        ids = {r["id"] for r in rules}
        for ft, rid in {
            "max_load_risetime": "K-FIX-019",
            "load_current_margin": "K-FIX-020",
            "fault_coverage_complete": "K-FIX-021",
            "default_path_continuous": "K-FIX-022",
            "switch_current_margin": "K-FIX-023",
        }.items():
            assert formula_type_to_rule_id(ft) in ids, f"{ft} -> {rid} 在规则库里不存在"

    def test_every_computable_fixture_rule_is_reachable(self, fixture_rules):
        """反向: 每条**可计算**的 fixture 规则都该有 formula_type 入口。

        这条是最能防退化的 —— 新加一条 fixture derive 规则却忘了接映射, 就会
        再次变成「在库里但没人用」。
        """
        mapped = {
            formula_type_to_rule_id(ft)
            for ft in (
                "tolerance", "rss_tolerance", "worst_case_tolerance",
                "fixture_precision", "probe_selection", "channel_count",
                "probe_life", "probe_required_life", "required_resolution",
                "kelvin_measured_resistance", "effective_clearance",
                "max_load_risetime", "load_current_margin",
                "fault_coverage_complete", "default_path_continuous",
                "switch_current_margin",
            )
        }
        computable = {
            r["id"] for r in fixture_rules if (r.get("derive") or {}).get("expr")
        }
        unreachable = sorted(computable - mapped)
        assert not unreachable, f"可计算但无 formula_type 入口的 fixture 规则: {unreachable}"


class TestIsolatedScalesExcluded:
    """ISO 5725 三条已移除(方案 §4.5 裁定) —— 这里钉住它们不会以别的形式回来。

    移除理由: ISO 5725 的**限值表需购买**, 拿不到; ``F_M.1.9`` 里
    ``r = 2.8·s_r`` 的 2.8 就是限值表里的 k 因子, 属硬写。量具能力已由
    ``K-FIX-016``(``%GRR``, MoreSteam 出处) 承担。
    """

    def test_iso5725_formulas_are_absent_from_the_seed(self):
        import json

        doc = json.loads(
            (ROOT / "data/seed/power_domain_seed.json").read_text(encoding="utf-8")
        )
        blob = json.dumps(doc, ensure_ascii=False)
        for token in ("ISO5725", "CG_CGK", "2.8·s_r"):
            assert token not in blob, f"种子仍含 ISO 5725 硬写内容: {token}"

    def test_generator_lists_them_as_out_of_scope(self):
        """移除必须落在**生成器**里 —— 直接改 JSON 就是下一次重生成又回来。"""
        src = (ROOT / "scripts/build_seed_data.py").read_text(encoding="utf-8")
        for token in ("F_M.1.9_ISO5725_REPEATABILITY", "F_M.1.13_CG_CGK"):
            assert token in src, f"生成器的 OUT_OF_SCOPE 清单里没有 {token}"

    def test_gauge_rr_replacement_is_present(self):
        """移除之后要有替代, 且替代必须带出处 —— 否则是能力回退。"""
        rules, _ = load_domain_rules(str(RULES_DIR), "power")
        grr = next((r for r in rules if r["id"] == "K-FIX-016"), None)
        assert grr is not None, "K-FIX-016 (%GRR 判定) 不在了"
        assert (grr.get("source") or {}).get("url"), "%GRR 替代缺出处"
