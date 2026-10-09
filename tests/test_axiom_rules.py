"""公理 -> 规则的裁定不变量(方案 §4.7)。

钉的是**可审计性**, 不是「有映射」: 一条只写 id 不写「哪条规则的哪句话」的
映射, 审计时无法复核, 等于没有映射 —— 所以 ``basis_detail`` 为空必须红。
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import knowledge_gate as kg  # noqa: E402

SEED = ROOT / "data" / "seed" / "power_domain_seed.json"
RULES = ROOT / "domain_rules" / "power" / "rules.yaml"

#: 方案原文里的公理规则编号形态。它们**一个都解析不到** —— 出现即红。
PLAN_MD_RULE_TOKEN = re.compile(r"^[RP]\d+$")

#: 需要写明「为什么这个候选不成立」的状态。方案原文: 「不接受光秃秃的『无』」。
NEEDS_REASON = frozenset(
    {
        "no_executable_counterpart",
        "formula_without_rule",
        "gate_blocked_by_policy",
        "excluded",
    }
)
#: 需要写明「规则的哪句话实现了它」的状态。
NEEDS_BASIS = frozenset({"mapped", "approximation"})

ALL_STATUSES = NEEDS_REASON | NEEDS_BASIS


@pytest.fixture(scope="module")
def axioms() -> list[dict]:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    return [r for r in data["records"] if r.get("entity_type") == "axiom"]


@pytest.fixture(scope="module")
def rule_ids() -> frozenset[str]:
    data = yaml.safe_load(RULES.read_text(encoding="utf-8-sig")) or {}
    return frozenset(r["id"] for r in data["rules"] if r.get("id"))


class TestAdjudicationIsComplete:
    def test_axiom_ids_have_no_gap(self, axioms: list[dict]) -> None:
        """A-1~A-15 一个不缺。

        A-11(量纲齐次)原先在库里缺席 —— 编号从 A-10 跳到 A-12。缺口的后果是
        审计读「A-1~A-15 全覆盖」会以为量纲门禁在库, 而它恰恰是全部公式的
        前置门禁。
        """
        nums = sorted(int(a["id"].split("-")[1]) for a in axioms)
        assert nums == list(range(1, 16)), nums

    def test_every_axiom_has_a_status(self, axioms: list[dict]) -> None:
        missing = [a["id"] for a in axioms if not a.get("status")]
        assert not missing, f"这些公理没有裁定结论: {missing}"
        bad = {a["id"]: a["status"] for a in axioms if a.get("status") not in ALL_STATUSES}
        assert not bad, f"未知 status 取值(改了取值域要改这里): {bad}"

    def test_counterpart_claims_carry_a_reason(self, axioms: list[dict]) -> None:
        """8 条 no_executable_counterpart 每条都要写明为什么。

        「找不到候选」和「原理上不可判定」是两回事, 都得说出来 —— 光秃秃的
        「无」让下一轮无从复核, 也让人分不清是漏找还是确实没有。
        """
        empty = [
            a["id"]
            for a in axioms
            if a.get("status") in NEEDS_REASON and not a.get("no_counterpart_reason")
        ]
        assert not empty, f"这些公理标了「无对应」却没写理由: {empty}"

    def test_mapped_claims_cite_the_rule_sentence(self, axioms: list[dict]) -> None:
        """能映射的必须指出「哪条规则的哪句话」。

        只给 id 不给依据, 审计无法复核 —— 与方案 §4.7「每条映射都要能回答
        哪条规则的哪句话实现了这个公理」同一条。
        """
        empty = [
            a["id"]
            for a in axioms
            if a.get("status") in NEEDS_BASIS
            and not (a.get("rule_adjudication") or {}).get("basis_detail")
        ]
        assert not empty, f"这些公理给了映射却没给依据: {empty}"

    def test_approximation_says_what_is_approximated(self, axioms: list[dict]) -> None:
        """标 approximate 的必须说明差在哪 —— 否则等于把近似冒充等同。"""
        approx = [a for a in axioms if a.get("status") == "approximation"]
        assert approx, "裁定表里应有近似映射(A-1 是), 没有就说明取值域被改动过"
        empty = [a["id"] for a in approx if not a.get("approximation")]
        assert not empty, f"标了近似却没说差在哪: {empty}"

    def test_excluded_records_the_reason_scope(self, axioms: list[dict]) -> None:
        """排除的必须写明是哪个范围被排除。"""
        exc = [a for a in axioms if a.get("status") == "excluded"]
        empty = [a["id"] for a in exc if not a.get("excluded_reason")]
        assert not empty, f"标了排除却没写范围: {empty}"


class TestRulesResolve:
    def test_no_plan_md_tokens_survive(self, axioms: list[dict]) -> None:
        """方案 md 的 R*/P* 编号一个都不该留下。

        那些记号是方案文档的行内编号, 不是本库任何 id; 留着就是悬空引用,
        而且知识门以前看不见它们(``rules`` 不在 REF_FIELDS 里)。
        """
        bad = [
            (a["id"], r)
            for a in axioms
            for r in (a.get("rules") or [])
            if PLAN_MD_RULE_TOKEN.match(str(r))
        ]
        assert not bad, f"方案 md 的裸编号残留: {bad}"

    def test_every_rules_value_is_a_real_rule(self, axioms: list[dict], rule_ids) -> None:
        bad = [
            (a["id"], r) for a in axioms for r in (a.get("rules") or []) if str(r) not in rule_ids
        ]
        assert not bad, f"指向规则库里不存在的规则: {bad}"

    def test_at_least_one_axiom_is_mapped(self, axioms: list[dict]) -> None:
        """裁定不是「全部无对应」。

        全空等于把公理与可执行规则的连接整个删掉 —— 那比悬空引用更坏, 因为
        它看起来干净。这条断言就是防「裁定表退化成全 no_executable_counterpart」。
        """
        mapped = [a for a in axioms if a.get("rules")]
        assert mapped, "没有任何公理连到可执行规则"


class TestTheoremNamespace:
    def test_theorems_field_carries_the_prefix(self, axioms: list[dict]) -> None:
        """``theorems`` 字段值必须带 ``thm::`` 前缀。

        边 ``has_theorem`` 的 target 是 ``thm::T1``, 字段以前存裸 ``T1`` ——
        同一对关系两种字符串写法, 查引用要认两种, 漏一种就是漏检。
        """
        bad = [
            (a["id"], t)
            for a in axioms
            for t in (a.get("theorems") or [])
            if not str(t).startswith("thm::")
        ]
        assert not bad, f"定理记号缺 thm:: 前缀: {bad}"

    def test_theorem_field_matches_edge_targets(self, axioms: list[dict]) -> None:
        """字段与边必须指向同一批定理节点。"""
        data = json.loads(SEED.read_text(encoding="utf-8"))
        edge_targets = collections.defaultdict(set)
        for r in data["records"]:
            if r.get("relationship_type") == "has_theorem":
                edge_targets[r["source_id"]].add(r["target_id"])
        for a in axioms:
            assert set(a.get("theorems") or []) == edge_targets.get(a["id"], set()), (
                f"{a['id']} 的 theorems 字段({a.get('theorems')})与 has_theorem 边"
                f"({sorted(edge_targets.get(a['id'], set()))})不一致"
            )


class TestNoDuplicateEdges:
    def test_relationship_pairs_are_unique(self) -> None:
        """(source, target) 不得重复。

        ContextGraph 按 (source, target) 去重, 所以重复边会让「边表条数」与
        「图里边数」对不上账 —— 这种差异常被误判成图构建有 bug。
        """
        data = json.loads(SEED.read_text(encoding="utf-8"))
        pairs = [
            (r["source_id"], r["target_id"]) for r in data["records"] if r.get("relationship_type")
        ]
        dupes = [p for p, n in collections.Counter(pairs).items() if n > 1]
        assert not dupes, f"重复的关系边: {dupes}"

    def test_applies_to_formula_is_gone(self) -> None:
        """``applies_to_formula`` 与 ``formula_refs`` 是同一语义两处表达。

        实测 3 条边与 3 条属性**完全一致**。边是属性的镜像, 留着则两边迟早
        漂移且说不清以哪个为准(红线 4)。
        """
        data = json.loads(SEED.read_text(encoding="utf-8"))
        kinds = collections.Counter(r.get("relationship_type") for r in data["records"])
        assert kinds.get("applies_to_formula", 0) == 0, dict(kinds)


class TestGateCoversTheNewFields:
    def test_rules_and_theorems_are_declared(self) -> None:
        assert "rules" in kg.REF_FIELDS
        assert "theorems" in kg.REF_FIELDS

    def test_prose_reason_fields_are_registered_as_non_reference(self) -> None:
        """裁定理由是散文, 不是引用列表。

        它们会提到别的 id(「那是电容电荷平衡, 属 A-5」), 但那是叙述里的提及。
        不登记的话通用扫描会把每条理由都报一遍。
        """
        for fld in ("approximation", "no_counterpart_reason", "excluded_reason"):
            assert fld in kg.EXTERNAL_FIELDS, fld

    def test_gate_resolves_k_tokens_against_the_rule_library(self) -> None:
        """``K-*`` 解析到 rules.yaml, 不是种子。

        种子不复制规则正文 —— 复制就是红线 4 的双源, 规则会改而种子不会跟着改。
        """
        assert len(kg.load_rule_ids()) > 0, "规则库 id 解析不到, 会让 K-* 全报悬空"
