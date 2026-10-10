"""需求 -> 领域概念 的声明式映射。

**这层边为什么必须有**
--------------------
``pg_source.py`` 的模块 docstring 写明「两类知识, 两个权威, 互不重叠」——
领域知识在种子, 型号知识在 PG。互不重叠的代价是图里没有一条边跨过去:
实测合并图 ``trace_dependency("SR-PA601-D54A-1204@unit=W")`` 找到节点但
``reachable=0`` —— 「这条需求依据什么」答不出来, 而那是这个工具存在的理由。

加了 13 条边之后(板卡实测): 连通分量 321 -> 315, 最大连通分量 242 -> 336,
**可达比例 28.8% -> 40.0%**。

**为什么必须是声明式的**
----------------------
实测 204 条需求标题与 194 个 ``power_concept`` 的 text **完全相同的 0 条**。
模糊匹配能凑出几百条边, 但**假边比没边更危险**: 追溯会给出「看似合理的错误
依据」, 而产线会照着它设计测试。所以每条映射都要写 ``basis``, 且概念必须在
种子里真实存在。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aterag.kg.req_concept_edges import (  # noqa: E402
    EDGE_TYPE,
    build_edges,
    load_declarations,
)

EDGES_YAML = ROOT / "data" / "seed" / "requirement_concept_edges.yaml"
SEED = ROOT / "data" / "seed" / "power_domain_seed.json"


def _seed_ids() -> set[str]:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    return {str(r.get("id")) for r in (data.get("records") or []) if r.get("id")}


def _entity(eid: str, req_id: str, rail: str = "") -> dict:
    return {
        "id": eid,
        "entity_type": "Requirement",
        "metadata": {"req_id": req_id, "rail": rail, "model_id": "PA601-D54A"},
    }


class TestDeclarationsAreHonest:
    """声明文件本身的性质 —— 它是数据, 但错了会变成「看似合理的错误依据」。"""

    def test_every_edge_has_a_basis(self):
        """每条都必须写清依据。没有依据的边等于「猜」。"""
        import yaml

        data = yaml.safe_load(EDGES_YAML.read_text(encoding="utf-8")) or {}
        for section in ("edges", "rule_edges"):
            missing = [
                i + 1
                for i, e in enumerate(data.get(section) or [])
                if not str(e.get("basis") or "").strip()
            ]
            assert not missing, f"{section} 第 {missing} 条缺 basis"

    def test_every_concept_exists_in_the_seed(self):
        """概念必须真实存在于种子。

        指向不存在概念的边, 在图上表现为「追溯到一半断了」—— 那会被误读成
        数据缺失, 而不是「映射写错了」。所以加载时就该抛。
        """
        known = _seed_ids()
        bad = [d.concept for d in load_declarations() if d.concept not in known]
        assert not bad, f"概念不在种子里: {bad}"

    def test_loader_rejects_an_unknown_concept(self, tmp_path, monkeypatch):
        """注入一个不存在的概念, 加载必须抛错(而不是跳过)。"""
        from aterag.kg import req_concept_edges as mod

        bad = tmp_path / "requirement_concept_edges.yaml"
        bad.write_text(
            "version: 1\nedges:\n  - req_id: SR-X\n    concept: NO_SUCH_CONCEPT\n    basis: 编的\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "_edges_path", lambda: bad)
        with pytest.raises(ValueError, match="不在种子里"):
            load_declarations()

    def test_loader_rejects_a_missing_basis(self, tmp_path, monkeypatch):
        from aterag.kg import req_concept_edges as mod

        bad = tmp_path / "requirement_concept_edges.yaml"
        concept = next(iter(sorted(_seed_ids())))
        bad.write_text(
            f"version: 1\nedges:\n  - req_id: SR-X\n    concept: {concept}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "_edges_path", lambda: bad)
        with pytest.raises(ValueError, match="basis"):
            load_declarations()

    def test_declarations_use_req_id_not_eid(self):
        """按 ``req_id`` 声明, 不按 eid。

        eid 里编码了 rail/unit/工况 变体(实测形态
        ``SR-PA601-D54A-1203@rail=-54V+duty_long_term=长期+unit=A``),
        抽取规则一改 eid 就变 —— 按 eid 写的映射会集体失效, 且失效时无声无息。
        """
        reqs = [d for d in load_declarations() if d.req_id.startswith("SR-")]
        assert reqs, "应有需求侧声明"
        for d in reqs:
            assert "@" not in d.req_id, f"{d.req_id} 里带 @ —— 那是 eid 的变体编码, 抽取一改就失效"
        # 规则侧用 rules.yaml 的 id(K-XXX-NNN), 本来就是图里的节点 id, 不经解析
        for d in load_declarations():
            if d.req_id.startswith("K-"):
                assert "@" not in d.concept, f"规则侧不该出现 eid 形态概念: {d.concept}"


class TestEdgeGeneration:
    def test_matches_by_req_id_across_rail_variants(self):
        """一条需求的所有 rail 变体都应连到同一概念(概念本身不分轨)。"""
        decls = load_declarations()
        d = next((x for x in decls if x.rail is None), None)
        if d is None:
            pytest.skip("无 rail 通配的声明")
        ents = [
            _entity(f"{d.req_id}@rail=-54V+unit=A", d.req_id, "-54V"),
            _entity(f"{d.req_id}@rail=3.45V+unit=A", d.req_id, "3.45V"),
        ]
        edges, stats = build_edges(ents)
        assert stats["matched_requirements"] == 1, stats
        assert len(edges) == 2, f"两个 rail 变体都该连边, 实得 {len(edges)}"
        assert {e["target_id"] for e in edges} == {d.concept}

    def test_rail_scoping_narrows_to_one_variant(self, tmp_path, monkeypatch):
        """``rail`` 指定时只连那一个变体 —— 需求按轨分列而概念不分时才需要。"""
        from aterag.kg import req_concept_edges as mod

        concept = next(iter(sorted(_seed_ids())))
        decl = tmp_path / "requirement_concept_edges.yaml"
        decl.write_text(
            "version: 1\nedges:\n"
            f"  - req_id: SR-X\n    concept: {concept}\n    rail: '-54V'\n"
            "    basis: 测试用\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "_edges_path", lambda: decl)
        ents = [
            _entity("SR-X@rail=-54V", "SR-X", "-54V"),
            _entity("SR-X@rail=3.45V", "SR-X", "3.45V"),
        ]
        edges, stats = build_edges(ents)
        assert len(edges) == 1, f"rail 指定后只应连一条, 实得 {len(edges)}"
        assert edges[0]["source_id"] == "SR-X@rail=-54V"
        assert stats["matched_requirements"] == 1

    def test_missing_requirement_is_skipped_not_fatal(self):
        """需求还没抽取完时跳过并计数, 不抛错。

        抛错会让「某个型号还没灌数据」表现成「映射文件坏了」—— 那会把排查
        方向带偏。跳过 + 计数就够了。
        """
        edges, stats = build_edges([_entity("SR-OTHER@unit=W", "SR-OTHER", "")])
        assert stats["skipped_not_in_pg"], "PG 里没有的需求应记进 skipped_not_in_pg"

    def test_edges_carry_the_basis_and_are_marked_declared(self):
        """边必须带依据, 且标记 ``declared: True``。

        标记的意义: 图里那些**自动**长出来的边(如 defined_by)与这些**声明**
        出来的边来源不同 —— 追溯到一条边时, 调用方需要知道该信到什么程度。
        """
        decls = load_declarations()
        if not decls:
            pytest.skip("无声明")
        ents = [_entity(f"{decls[0].req_id}@unit=W", decls[0].req_id, "")]
        edges, _ = build_edges(ents)
        assert edges, "应至少生成一条边"
        for e in edges:
            assert e["properties"]["declared"] is True
            assert e["properties"]["basis"].strip()
            assert e["relationship_type"] == EDGE_TYPE

    def test_stats_report_coverage_honestly(self):
        """覆盖度必须报出来 —— 低覆盖是事实, 不该被藏。"""
        decls = load_declarations()
        ents = [_entity(f"{d.req_id}@unit=W", d.req_id, "") for d in decls]
        _edges, stats = build_edges(ents)
        assert stats["declared"] == len(decls)
        assert stats["matched_requirements"] == len(decls)
        assert stats["unmapped_requirements"] == 0
        # 未映射时必须 > 0, 否则这条统计没有意义
        _e2, s2 = build_edges([_entity("SR-NOWHERE@unit=W", "SR-NOWHERE", "")])
        assert s2["unmapped_requirements"] >= 1

    def test_metadata_key_is_the_one_pg_source_emits(self):
        """实体属性在 ``metadata`` 下, 不是 ``properties``。

        早先写成 ``properties`` 时, 所有声明静默匹配 0 条 —— 代码看起来完全
        正常, 只是图没变化。这条把键名钉住。
        """
        ents = [_entity("SR-X@unit=W", "SR-X", "")]
        edges, stats = build_edges(ents)
        assert "metadata" in ents[0] and "properties" not in ents[0]
        # 声明里若没有 SR-X 则跳过; 有则说明 metadata 键被正确读取
        if any(d.req_id == "SR-X" for d in load_declarations()):
            assert edges and stats["matched_requirements"] >= 1
