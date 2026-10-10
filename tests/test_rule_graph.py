"""规则层进分析图。

**为什么要加这一层**
------------------
板卡实测: ``domain_rules/power/rules.yaml`` 的 130 条规则, 与种子实体的 id
**重叠 0 条**, statement 前 20 字能在种子实体名里找到的也是 0 条。也就是
分析图里**一条规则都没有** ——

* ``trace_dependency("K-ELEC-001")`` 返回 ``found=false``
* ``analyze_graph`` 的 degree 排名前列全被 ``std::`` 标准实体占满(标准是种子
  边里唯一有连接的一类), 因为真正连得最密的骨架缺席

**为什么复用而不是重写**
----------------------
``scripts/sync_semantica.py`` 早就有 ``build_knowledge_graph`` 在做同一件事,
但返回 semantica 容器格式, 与 ``collect_records`` 要的记录格式不同。两份实现
必然漂: 同一批规则在 AGE 与在分析图里长成两个样子时, 没人说得清哪个是真的。
所以规则记录的构造只在 :mod:`aterag.kg.rule_graph` 一处。

**实测效果**
------------
    图规模        841 -> 1267 节点 / 520 -> 1057 边
    最大连通分量  242 -> 713
    可达比例      28.8% -> 56.3%(纯种子基线是 8.1%)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aterag.kg.rule_graph import (  # noqa: E402
    CAT_PREFIX,
    ENTITY_TYPE_RULE,
    FORMULA_PREFIX,
    SCOPE_PREFIX,
    build_rule_records,
    load_domain_rule_records,
)


@pytest.fixture(scope="module")
def rules() -> list[dict]:
    import yaml

    data = yaml.safe_load((ROOT / "domain_rules/power/rules.yaml").read_text(encoding="utf-8"))
    return data["rules"]


class TestRuleRecords:
    def test_every_rule_becomes_a_node(self, rules):
        ents, _rels = build_rule_records(rules, domain="power")
        ids = {e["id"] for e in ents if e["entity_type"] == ENTITY_TYPE_RULE}
        want = {str(r["id"]) for r in rules if r.get("id")}
        assert ids == want, (
            f"规则节点缺失: 少 {sorted(want - ids)[:5]} / 多 {sorted(ids - want)[:5]}"
        )
        assert len(want) == 130, f"规则数变了: {len(want)}(基线 130)"

    def test_derive_and_constraint_edges_exist(self, rules):
        _e, rels = build_rule_records(rules, domain="power")
        by_type: dict[str, int] = {}
        for r in rels:
            by_type[r["relationship_type"]] = by_type.get(r["relationship_type"], 0) + 1
        # 53 条 derive / 77 条 SHACL constraint, 与 rules.yaml 的结构对应
        assert by_type.get("HAS_FORMULA") == 53, by_type
        assert by_type.get("HAS_CONSTRAINT") == 77, by_type
        assert by_type.get("BELONGS_TO") == 130, by_type
        assert by_type.get("IN_SCOPE") == 130, by_type

    def test_rule_ids_do_not_collide_with_seed_ids(self, rules):
        """规则侧 id 加前缀, 避免与种子 id 撞。

        撞 id 会静默合并成一个节点, 属性来自其中一方 —— 那是知识层最坏的
        失败: 界面看起来正常, 但一个节点的知识被吞掉且不可见。
        """
        import json

        seed_ids = {
            str(r.get("id"))
            for r in json.loads(
                (ROOT / "data/seed/power_domain_seed.json").read_text(encoding="utf-8")
            )["records"]
            if r.get("id")
        }
        ents, _rels = build_rule_records(rules, domain="power")
        rule_ids = {e["id"] for e in ents if e["entity_type"] == ENTITY_TYPE_RULE}
        assert not (rule_ids & seed_ids), f"规则 id 与种子撞了: {sorted(rule_ids & seed_ids)[:5]}"
        for e in ents:
            if e["entity_type"] != ENTITY_TYPE_RULE:
                assert e["id"].startswith(
                    (CAT_PREFIX, SCOPE_PREFIX, "src:", FORMULA_PREFIX, "shape:")
                ), f"辅助节点缺前缀, 可能撞 id: {e['id']}"

    def test_rule_metadata_keeps_what_reasoning_needs(self, rules):
        ents, _ = build_rule_records(rules, domain="power")
        by_id = {e["id"]: e for e in ents if e["entity_type"] == ENTITY_TYPE_RULE}
        k1 = by_id["K-ELEC-001"]
        md = k1["metadata"]
        assert md["derive_output"] == "power"
        assert md["derive_expr"] == "voltage * current"
        assert md["rule_kind"] == "derive"
        assert md["test_expect"], "回归用的 test 期望值必须带进来"
        assert md["source_url"], "出处必须带进来 —— 无出处的规则不该进知识图"

    def test_rule_without_id_is_skipped_not_silently_kept(self):
        """缺 id 的规则跳过 —— 但**不能假装没有**。

        若悄悄生成一个匿名 id, 那条规则就会以一个没人能引用的名字进图,
        而外面看到的仍是「130 条规则都在」。
        """
        ents, _rels = build_rule_records(
            [{"id": "K-X-001", "statement": "有 id 的"}, {"statement": "没 id 的"}],
            domain="power",
        )
        rule_ids = {e["id"] for e in ents if e["entity_type"] == ENTITY_TYPE_RULE}
        assert rule_ids == {"K-X-001"}


class TestRuleFileLoading:
    def test_missing_rules_file_raises_with_a_real_reason(self, tmp_path):
        """规则文件不在时抛错, 不是返回空。

        空规则层会让「追溯不到 K-ELEC-001」表现成数据缺失, 而真实原因是
        文件没找到 —— 那会把排查方向带偏。
        """
        with pytest.raises(FileNotFoundError, match="规则文件不存在"):
            load_domain_rule_records(tmp_path, "power")

    def test_loads_the_real_rules_file(self):
        ents, rels, stats = load_domain_rule_records(ROOT / "domain_rules", "power")
        assert stats["rules"] == 130
        assert stats["entities"] > stats["rules"], "还应有 category/scope/source/formula 节点"
        assert stats["relations"] >= 520
        # 声明的「规则 -> 概念」边也一并挂上
        assert stats["concept_edges"] >= 1, "声明的规则->概念边没有生效"
