"""知识图谱物化层的不变量测试。

范围: 纯内存, 不碰 PG。PG 侧由 ``test_kg_pg_source.py`` 用假 cursor 覆盖。

每条测试名里写清「原本坏在哪」—— 这些断言对应的是**真实发生过的缺陷**,
不是假想的:
  * 外部本体引用(``qudt:*``)曾被当成本地边建出来 -> 24 条指向虚空的边
  * ``std::X defined_by std::X`` 自环曾被建出来 -> 10 条无信息边
  * 型号 Product 节点曾与抽取出的 Product 实体重 id -> 属性取决于遍历顺序
"""

from __future__ import annotations

from typing import Any

import pytest

from aterag.kg.materialize import build_context_graph


def _node(node_id: str, node_type: str = "power_concept", **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "entity_type": node_type, "text": node_id, **extra}


def _rel(src: str, tgt: str, rtype: str, **extra: Any) -> dict[str, Any]:
    return {"source_id": src, "target_id": tgt, "relationship_type": rtype, **extra}


def _edge_types(graph) -> set[tuple[str, str, str]]:
    """``graph.edges`` 是 ``List[ContextEdge]``(不是 dict)。"""
    return {
        (str(e.source_id), str(e.target_id), str(e.edge_type))
        for e in graph.edges
    }


# ---------------------------------------------------------------------------
# 外部本体引用: 不是本地边
# ---------------------------------------------------------------------------


class TestExternalOntologyRefs:
    """``external: true`` 的关系指向**外部本体**, 不建本地边。

    生成器在 ``build_seed_data.py:930`` 写死了这个语义:「指向外部本体, 不是
    本图节点 —— 用 IRI 形式, 不伪造本地 id」。建边就会得到指向虚空的边,
    Explorer 里点开是空页。
    """

    def test_external_ref_becomes_no_edge(self) -> None:
        graph, n_nodes, n_edges = build_context_graph(
            [
                _node("VOUT_RIPPLE"),
                _rel("VOUT_RIPPLE", "qudt:PotentialDifference", "has_unit_kind",
                     external=True, ontology="QUDT"),
            ],
            [],
        )
        assert n_nodes == 1
        assert n_edges == 0, "外部本体引用被错误地建成了本地边"
        assert not _edge_types(graph)

    def test_external_ref_kept_on_source_node(self) -> None:
        """信息不能丢 —— 折进源节点的 metadata。"""
        graph, _, _ = build_context_graph(
            [
                _node("VOUT_RIPPLE"),
                _rel("VOUT_RIPPLE", "qudt:PotentialDifference", "has_unit_kind",
                     external=True, ontology="QUDT"),
            ],
            [],
        )
        attrs = graph.get_node_attributes("VOUT_RIPPLE")
        assert attrs.get("external_refs") == {"qudt:PotentialDifference": "QUDT"}

    def test_non_external_relation_still_becomes_edge(self) -> None:
        """不能把这条规则写得太宽 —— 普通关系照旧建边。"""
        graph, _, n_edges = build_context_graph(
            [_node("A"), _node("B"), _rel("A", "B", "defined_by")],
            [],
        )
        assert n_edges == 1
        assert ("A", "B", "defined_by") in _edge_types(graph)

    def test_real_seed_keeps_qudt_out_of_the_graph(self) -> None:
        """对真实种子复核: ``qudt:*`` 一次都不该出现在图的节点/边里。"""
        import json
        from pathlib import Path

        seed = Path("data/seed/power_domain_seed.json")
        if not seed.exists():
            pytest.skip("种子文件不在(离线包/裁剪仓库里)")
        records = json.loads(seed.read_text(encoding="utf-8"))["records"]
        graph, _, _ = build_context_graph(records, [])

        for nid in graph.nodes:
            assert not str(nid).startswith("qudt:"), f"凭空造出了本地 QUDT 节点: {nid}"
        for src, tgt, _t in _edge_types(graph):
            assert not str(tgt).startswith("qudt:"), f"边指向不存在的 QUDT 节点: {tgt}"

    def test_real_seed_carries_no_external_relations(self) -> None:
        """真实种子里 **一条 ``external: true`` 关系都没有** —— 上面那个断言平凡通过。

        ``prune_non_executable`` 的双端存活过滤会在生成阶段就把
        ``has_unit_kind`` -> ``qudt:*`` 整条丢掉(``qudt:*`` 不在实体集里), 所以
        :meth:`test_external_ref_kept_on_source_node`` 那两个测试是**手工构造**
        记录、与生成器实际产出脱钩的 —— 它们测的是函数, 不是接线。

        这条测试把接线本身钉住: 若哪天有人放宽那个过滤(比如想让 QUDT 引用真的
        进图), ``external_refs`` 的兜底逻辑就必须同时被重新审视, 否则上面那些
        测试还会照样绿, 而 ``external_refs`` 仍是空的。
        """
        import json
        from pathlib import Path

        seed = Path("data/seed/power_domain_seed.json")
        if not seed.exists():
            pytest.skip("种子文件不在(离线包/裁剪仓库里)")
        records = json.loads(seed.read_text(encoding="utf-8"))["records"]
        rels = [r for r in records if isinstance(r, dict) and r.get("source_id")]
        ext = [r for r in rels if r.get("external") is True]
        assert not ext, (
            f"种子里出现了 {len(ext)} 条 external 关系(例如 {ext[0]})—— "
            f"materialize 的 external_refs 兜底路径从不被走, "
            f"而它的注释曾声称「实测 24 条」"
        )

    def test_qudt_ref_attributes_survive_as_plain_attributes(self) -> None:
        """``qudt_ref`` 作为**概念属性**仍在库里 —— 这才是那条信息真正的落点。

        关系被过滤掉不等于信息丢失: 单位挂在概念的属性上。哪天有人看到
        「``qudt:`` 边全没了」就顺手把 ``qudt_ref`` 属性也删掉, 那才是真丢信息。
        """
        import json
        from pathlib import Path

        seed = Path("data/seed/power_domain_seed.json")
        if not seed.exists():
            pytest.skip("种子文件不在(离线包/裁剪仓库里)")
        records = json.loads(seed.read_text(encoding="utf-8"))["records"]
        with_ref = [
            r
            for r in records
            if isinstance(r, dict)
            and r.get("entity_type") == "power_concept"
            and r.get("qudt_ref")
        ]
        assert with_ref, "所有概念的 qudt_ref 都没了 —— 单位信息已丢失"


# ---------------------------------------------------------------------------
# 自环: 丢弃
# ---------------------------------------------------------------------------


class TestSelfLoops:
    """``source_id == target_id`` 的边不含任何信息。

    实测 10 条, 全部是 ``std::X defined_by std::X`` 且 ``clause`` 就是标准号
    本身 —— 那是 ``standards_add`` 逐条发关系时落下的产物, 不是语义。
    """

    def test_self_loop_dropped(self) -> None:
        _graph, _, n_edges = build_context_graph(
            [_node("std::GB 4943.1-2022", "standard"), _rel("std::GB 4943.1-2022",
                                                            "std::GB 4943.1-2022", "defined_by")],
            [],
        )
        assert n_edges == 0

    def test_real_seed_has_no_self_loop(self) -> None:
        import json
        from pathlib import Path

        seed = Path("data/seed/power_domain_seed.json")
        if not seed.exists():
            pytest.skip("种子文件不在")
        records = json.loads(seed.read_text(encoding="utf-8"))["records"]
        _graph, _, _ = build_context_graph(records, [])
        for src, tgt, _t in _edge_types(_graph):
            assert src != tgt, f"图里仍有自环: {src}"


# ---------------------------------------------------------------------------
# 悬空边: 丢弃
# ---------------------------------------------------------------------------


class TestDanglingEdges:
    """目标不在节点集里的边必须丢, 否则 Explorer 邻接返回一个空节点。"""

    def test_edge_to_missing_target_dropped(self) -> None:
        _graph, _, n_edges = build_context_graph(
            [_node("A"), _rel("A", "NO_SUCH_NODE", "defined_by")],
            [],
        )
        assert n_edges == 0

    def test_real_seed_has_no_dangling_edge(self) -> None:
        import json
        from pathlib import Path

        seed = Path("data/seed/power_domain_seed.json")
        if not seed.exists():
            pytest.skip("种子文件不在")
        records = json.loads(seed.read_text(encoding="utf-8"))["records"]
        graph, _, _ = build_context_graph(records, [])
        for src, tgt, _t in _edge_types(graph):
            assert graph.has_node(str(src)), f"边的源节点不存在: {src}"
            assert graph.has_node(str(tgt)), f"边的目标节点不存在: {tgt}"


# ---------------------------------------------------------------------------
# 出处必须透传
# ---------------------------------------------------------------------------


class TestProvenancePassthrough:
    """出处是追溯审计的底座, 物化时必须原样带过去。"""

    def test_standard_authority_kept(self) -> None:
        graph, _, _ = build_context_graph(
            [_node("HYSTERESIS", authority_kind="standard", source="YD/T 1817-2017", confidence=0.9)],
            [],
        )
        a = graph.get_node_attributes("HYSTERESIS")
        assert a["authority_kind"] == "standard"
        assert a["source"] == "YD/T 1817-2017"
        assert a["confidence"] == 0.9

    def test_missing_source_stays_empty_string(self) -> None:
        """「明确没有出处」与「出处未知」是两回事, 空串必须保留而不是省略。"""
        graph, _, _ = build_context_graph([_node("X")], [])
        assert graph.get_node_attributes("X")["source"] == ""

    def test_clause_kept_when_present(self) -> None:
        graph, _, _ = build_context_graph([_node("X", clause="3.10")], [])
        assert graph.get_node_attributes("X")["clause"] == "3.10"


# ---------------------------------------------------------------------------
# 幂等: 重复节点 id 不应静默产生两份
# ---------------------------------------------------------------------------


class TestIdHandling:
    def test_duplicate_id_keeps_one_node(self) -> None:
        """重复 id 合并成一个节点(而不是报错)。

        这里不报错是有意的: 种子与 PG 合成时, 型号的 Product 节点可能与领域
        知识里的同 id 项撞上, 那时该由上游决定谁权威, 而不是让图谱构建失败。
        PG 侧的**跨型号**冲突才是硬错误, 见 ``test_kg_pg_source.py``。
        """
        _graph, n_nodes, _ = build_context_graph(
            [_node("DUP", "power_concept"), _node("DUP", "model/Product")],
            [],
        )
        assert n_nodes == 1
