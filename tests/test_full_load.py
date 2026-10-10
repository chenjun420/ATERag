"""满载基准值 ``value_at_full_load`` 的生产方。

**它补的是什么缺口**
------------------
``seed.json`` 的 ``facts`` 里只有 4 条 ``load_ratio`` / ``load_alias``, 没有
``value_at_full_load``。于是 ``value_at_load(Quantity, Load, ?, Ratio)`` 的
第四元组永远是空的 —— 测试里能算出半载 299.7W 是因为**测试自己手工喂了那条
事实**, 生产链路上没有喂事实的地方。

**两种口径同时成立**
-------------------
PA601「满载输出功率」有两个都站得住的数, 且**用途不同**:

    600.0 W  规格上限(SR-1204 的 max)      —— 合格判定用它
    599.4 W  K-ELEC-001 由 54V x 11.1A 推导  —— 算某工作点用它

早先我把它们当成「必须选一个」的冲突, 那是错误的框 —— 那是两个来源、两种用途
的量。产线两个都要, 所以两者作为同一 Quantity 下的不同事实同时产出。

**推导口径只认显式声明**
----------------------
不能把 130 条规则的 ``test.expect`` 全收进来: 那是规则的**自校验输入**,
不是产品的满载值。反例 —— K-ELEC-003(欧姆定律 电流=U/R) 的
``test.expect.current`` 是 **2.0**, 而 PA601 满载输出电流是 **11.1A**。
把 2.0 当满载值, 会让「半载输出电流 = 1.0A」这个错数看起来有完整推导过程 ——
那比没有更危险。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aterag.reasoning.full_load import (  # noqa: E402
    DERIVED_SUFFIX,
    FullLoadFact,
    build_all,
    from_spec_limits,
)

SEED = ROOT / "data" / "seed" / "power_domain_seed.json"


def _req(eid: str, req_id: str, title: str, vmax: object, unit: str = "", rail: str = "") -> dict:
    return {
        "id": eid,
        "entity_type": "Requirement",
        "metadata": {
            "etype": "Requirement",
            "req_id": req_id,
            "title": title,
            "max": vmax,
            "unit": unit,
            "rail": rail,
        },
    }


class TestSpecLimits:
    def test_max_becomes_a_fact_with_its_source(self):
        facts = from_spec_limits([_req("SR-X@unit=W", "SR-X", "输出功率", 600.0, "W")])
        assert len(facts) == 1
        f = facts[0]
        assert f.value == 600.0
        assert "SR-X" in f.basis
        assert f.source_id == "SR-X"

    def test_requirement_without_max_is_skipped(self):
        """没有上限就没有「满载值」可言。

        凭空补一个 0 会让半载也变成 0, 而那看起来像「这一档就是 0」。
        """
        assert from_spec_limits([_req("SR-X", "SR-X", "开机输出延迟", None)]) == []

    def test_non_numeric_max_is_skipped_not_coerced(self):
        assert from_spec_limits([_req("SR-X", "SR-X", "输出功率", "约600")]) == []

    def test_tiered_limits_are_kept_both(self):
        """分档位是事实, 去重成一条会把档位信息抹掉。

        PA601 输出功率按输入电压分档(90~176Vac: 400W / 176~286Vac: 600W)。
        """
        facts = from_spec_limits(
            [
                _req("SR-X@unit=W#1", "SR-X", "输出功率", 400.0, "W"),
                _req("SR-X@unit=W#2", "SR-X", "输出功率", 600.0, "W"),
            ]
        )
        assert {f.value for f in facts} == {400.0, 600.0}

    def test_rail_variants_are_both_kept(self):
        facts = from_spec_limits(
            [
                _req("SR-Y@rail=-54V", "SR-Y", "输出电流", 11.1, "A", "-54V"),
                _req("SR-Y@rail=3.45V", "SR-Y", "输出电流", 0.1, "A", "3.45V"),
            ]
        )
        assert {f.value for f in facts} == {11.1, 0.1}


class TestBothCalibersCoexist:
    def test_same_quantity_carries_both_calibers(self):
        """同一 Quantity 下两种口径都在 —— 这正是「同时做」的意思。"""
        facts = [
            FullLoadFact("YC_POUT", 600.0, "SR-1204 max", "spec", "W", "SR-PA601-D54A-1204"),
            FullLoadFact("YC_POUT", 599.4, "K-ELEC-001", "derived", "W", "K-ELEC-001"),
        ]
        by_q: dict[str, set[float]] = {}
        for f in facts:
            by_q.setdefault(f.quantity, set()).add(f.value)
        assert by_q["YC_POUT"] == {600.0, 599.4}

    def test_derived_suffix_marks_the_caliber(self):
        """口径后缀写进名字 —— 消费方按名字取, 不靠猜。"""
        assert DERIVED_SUFFIX == "_derived"


class TestFactText:
    def test_fact_is_quoted(self):
        """必须带引号: 裸标识符会被当变量而抛错。"""
        f = FullLoadFact("YC_POUT", 599.4, "b", "derived")
        assert f.fact_str == 'value_at_full_load("YC_POUT", 599.4)'

    def test_round_trips_through_the_real_engine(self):
        """喂进真实 DatalogReasoner 并查出四元组 —— 端到端而非只测字符串。"""
        from semantica.reasoning.datalog_reasoner import DatalogReasoner

        seed = json.loads(SEED.read_text(encoding="utf-8"))
        r = DatalogReasoner()
        for f in seed["facts"]:
            r.add_fact(f["fact_str"])
        for rule in seed["rules"]:
            r.add_rule(rule["rule_str"])
        for f in (
            FullLoadFact("YC_POUT", 600.0, "SR-1204", "spec", "W", "SR-PA601-D54A-1204"),
            FullLoadFact("YC_POUT", 599.4, "K-ELEC-001", "derived", "W", "K-ELEC-001"),
        ):
            r.add_fact(f.fact_str)

        rows = r.query('value_at_load("YC_POUT", half_load, F, Rt)')
        assert len(rows) == 2, f"两个口径都该推出四元组, 实得 {rows}"
        assert {str(x["F"]) for x in rows} == {"600.0", "599.4"}
        assert {str(x["Rt"]) for x in rows} == {"0.5"}


class TestDeclaredDerivation:
    def test_only_declared_rules_produce_derived_facts(self, rules):
        """未声明的规则不产出推导口径 —— 自测基准不等于产品满载值。"""
        facts, stats = build_all(rules, [])
        assert stats["derived"] == 1, f"只有显式声明的规则该产出推导口径, 实得 {stats['derived']}"

    def test_the_declared_rule_is_k_elec_001(self, rules):
        facts, _ = build_all(rules, [])
        assert [f.source_id for f in facts] == ["K-ELEC-001"]
        assert facts[0].value == 599.4

    def test_ohm_law_test_expect_is_not_treated_as_full_load(self, rules):
        """K-ELEC-003 的 test.expect.current 是 2.0, 不是满载 11.1A。

        实测这个值来自欧姆定律的自校验输入(任意电阻值), 与产品满载无关。
        """
        facts, _ = build_all(rules, [])
        assert not any(f.value == 2.0 for f in facts), "把规则自测基准当成了满载值"

    def test_both_calibers_present_for_output_power(self, rules):
        facts, stats = build_all(
            rules, [_req("SR-1204@unit=W", "SR-PA601-D54A-1204", "输出功率", 600.0, "W")]
        )
        pout = [f for f in facts if f.quantity == "YC_POUT"]
        assert {f.authority_kind for f in pout} == {"derived", "spec"}, pout
        assert stats["derived_renamed_to_concept"] >= 1
        assert stats["spec_renamed_to_concept"] >= 1

    def test_renaming_uses_source_id_not_text_matching(self, rules):
        """归名按 ``source_id`` 字段, 不按 basis 文本切词。

        实测按空格切词匹配不上 —— ``SR-X「输出功率」的 max=600W`` 是**一个**
        token, 于是归名静默失效(spec 侧归名 0 条), 两种口径对不上号。
        """
        facts, stats = build_all(
            rules, [_req("SR-1204@unit=W", "SR-PA601-D54A-1204", "输出功率", 600.0, "W")]
        )
        assert any(f.quantity == "YC_POUT" for f in facts), stats


@pytest.fixture(scope="module")
def rules() -> list[dict]:
    import yaml

    return yaml.safe_load((ROOT / "domain_rules/power/rules.yaml").read_text(encoding="utf-8"))[
        "rules"
    ]
