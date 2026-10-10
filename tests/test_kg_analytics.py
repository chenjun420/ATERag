"""图分析层 (方案 §4.4) 的不变量测试。

**这一节最要紧的是「诚实」**: 早先的种子极稀疏 (2026-09 时是「641 节点 / 65 边 /
87% 孤立」), 补数据后已降到 2026-10-08 实测 594 节点 / 275 边 / 47.5% 孤立。

而 47.5% 恰好落在 ``SPARSE_GRAPH_RATIO = 0.5`` **之下** —— 于是按单一判据,
``sparseness_warning`` 从此再不触发, 而图其实仍然是碎的: 594 个节点散成
**319 个连通分量**, 最大连通分量只有 48 个(8.1%)。只报「孤立率」的判据
看不见这件事, 而它恰恰是决定「追溯能看多远」的那个数。

所以判据现在是**两条**(孤立率 / 可达比例), 下面的测试断言的是**规则**
(警告有无必须与实测拓扑一致), 不是某个固定数字 —— 钉死数字等于把数据
现状写成契约, 数据一改就报假失败。别照抄注释里的数字, 实测
``analytics.topology()``。

中心性与追溯的产出天然信息量有限。所以测试钉的不是「算出了洞察」, 而是:

1. 产出**如实反映稀疏度** —— 输出里必须带 sparsity 说明, 否则通过 MCP 拿到一片
   0 排名的人会以为那些标准不重要, 而事实是它们只是没进任何边。
2. **方向语义正确** —— ``has_theorem`` 边上「谁推出 T1」必须反着走。方向读反
   不报错, 只给出一个看起来合理的空答案, 那种错最难发现。
3. 找不到节点时给**可操作**的提示, 不是「共 N 个节点」这种没用的话。
4. 图建不起来判 ERROR 而不是跳过 —— 跳过会把「查不了」报成「没问题」。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from aterag.kg import analytics  # noqa: E402
from aterag.kg.materialize import build_context_graph, load_seed_records  # noqa: E402

SEED = ROOT / "data/seed/power_domain_seed.json"


def _node(nid: str, ntype: str = "concept") -> dict:
    return {"id": nid, "entity_type": ntype, "text": nid}


def _rel(src: str, tgt: str, etype: str = "defined_by") -> dict:
    return {"source_id": src, "target_id": tgt, "relationship_type": etype}


def _graph(records):
    return build_context_graph(records, [])[0]


def _should_warn(topo: dict) -> bool:
    """两条判据的「或」—— 与 ``server._sparseness_note`` 的触发条件一致。

    单独抽出来是因为两个工具(analyze_graph / trace_dependency)都要断言同一条规则,
    而它们拿到的 topology 来源不同(一个在返回值里, 一个要自己算)。规则写两遍
    就会漂 —— 早先这文件里就有过「注释说 47.5% 孤立、断言按 50% 阈值走」的错位。
    """
    from aterag.mcp_server import server

    n = topo.get("nodes") or 0
    if not n:
        return False
    return (
        topo.get("isolated_nodes", 0) / n > server.SPARSE_GRAPH_RATIO
        or topo.get("largest_component", 0) / n < server.FRAGMENTED_GRAPH_RATIO
    )


@pytest.fixture(scope="module")
def seed_graph():
    ents, rels = load_seed_records(str(SEED))
    return analytics.graph_from_records([*ents, *rels])


class TestAdapter:
    def test_direction_is_preserved(self):
        """**有向**图: ``defined_by`` 反过来读是错的。

        用无向图会让「依赖追溯」把方向读反, 而那种错误不会报错, 只会给出看起来
        合理的反向答案。
        """
        g = _graph([_node("A"), _node("B"), _rel("A", "B")])
        nxg = analytics._to_networkx(g)
        assert nxg.has_edge("A", "B")
        assert not nxg.has_edge("B", "A"), "关系被当成无向边了"

    def test_validator_payload_carries_required_fields(self):
        """``GraphValidator`` 要求实体有 name/type、关系有 type。

        少给就报出 700 条同款「missing required fields」, 真正的结构问题被淹没。
        """
        g = _graph([_node("A"), _node("B"), _rel("A", "B")])
        payload = analytics._from_networkx(analytics._to_networkx(g))
        for e in payload["entities"]:
            assert e["name"] and e["type"]
        for r in payload["relationships"]:
            assert r["type"], "关系缺 type -> validator 会报 missing required fields"

    def test_real_seed_validates_without_false_issues(self, seed_graph):
        """真实种子不该因为适配层的错而报出一堆假 issue。"""
        v = analytics.validate_structure(seed_graph)
        msgs = " ".join(i["message"] for i in v["issues"])
        assert "missing required fields" not in msgs, f"适配层漏字段: {msgs[:200]}"


class TestValidateStructure:
    def test_returns_topology_numbers(self, seed_graph):
        """拓扑数字必须是**可断言的返回值**, 不能只是 print 出来的。"""
        v = analytics.validate_structure(seed_graph)
        t = v["topology"]
        assert t["nodes"] > 0
        assert t["edges"] >= 0
        assert t["isolated_nodes"] <= t["nodes"]
        assert t["largest_component"] >= 1

    def test_orphan_nodes_are_reported_as_warning_not_error(self, seed_graph):
        """孤立节点是「关系没建够」, 不是「数据坏了」。"""
        v = analytics.validate_structure(seed_graph)
        sevs = {i["severity"] for i in v["issues"]}
        assert not any("ERROR" in s for s in sevs), f"孤立节点被判成了 ERROR: {v['issues'][:3]}"


class TestCentrality:
    def test_scores_all_nodes(self, seed_graph):
        """每个节点都要拿到分数 —— 漏掉的就成了「看起来不重要」。

        注意这里用 ``topology()`` 单独取节点数: ``centrality_report`` 只管排名,
        拓扑数字是另一件事(测试 ``test_report_has_no_topology_key`` 钉住了这个分工)。
        """
        c = analytics.centrality_report(seed_graph)
        n = analytics.topology(seed_graph)["nodes"]
        for m in ("degree", "pagerank"):
            assert c["metrics"][m]["scored_nodes"] == n, f"{m} 只给 {c['metrics'][m]} 个节点打了分"

    def test_report_has_no_topology_key(self, seed_graph):
        """职责分工: 拓扑归 ``topology()``, 排名归 ``centrality_report``。

        两件事混在一个返回里, 调用方就会只取自己需要的那个而丢掉另一个 ——
        而「稀疏度」恰恰是最该被一起带出去的那个。
        """
        assert "topology" not in analytics.centrality_report(seed_graph)

    def test_top_list_is_sorted_descending(self, seed_graph):
        c = analytics.centrality_report(seed_graph)
        for m, top in c["top"].items():
            scores = [x["score"] for x in top]
            assert scores == sorted(scores, reverse=True), f"{m} 的排名不是降序"

    def test_top_nodes_are_real_nodes_in_the_graph(self, seed_graph):
        c = analytics.centrality_report(seed_graph)
        nxg = analytics._to_networkx(seed_graph)
        for m, top in c["top"].items():
            for x in top:
                assert x["node"] in nxg, f"{m} 排名里出现了图里没有的节点 {x['node']}"

    def test_thin_coverage_is_a_direct_action_item(self, seed_graph):
        """排名要人自己找阈值, 行动项不该再让人找一次。"""
        c = analytics.centrality_report(seed_graph)
        thin = c["thin_covered"]
        assert thin["total"] > 0
        assert thin["count"] > 0, "PA601 确实有大量零入度标准 —— 断言它存在"
        assert thin["zero_in_degree"] <= thin["count"]
        nxg = analytics._to_networkx(seed_graph)
        for item in thin["thin"]:
            assert item["node"] in nxg

    def test_thin_threshold_is_quantile_not_hardcoded(self):
        """阈值写死「只被 1 个引用」在节点数变化时要么全中要么全不中。

        构造 10 个标准、只有 5 个被引用: 固定阈值 1 会把「0 次」和「1 次」混成
        一档, 而分位数能把「零引用」单独拎出来 —— 那才是真正的覆盖薄弱。
        """
        recs = [_node(f"C{i}", "concept") for i in range(10)]
        recs += [_node(f"S{i}", "standard") for i in range(10)]
        recs += [_rel(f"C{i}", f"S{i}") for i in range(5)]
        thin = analytics.thin_coverage(_graph(recs))
        assert thin["total"] == 10
        assert thin["threshold"] == 0, "零引用的那 5 个应被判为薄覆盖"
        assert thin["count"] == 5
        assert thin["zero_in_degree"] == 5

    def test_no_targets_returns_empty_not_crash(self):
        g = _graph([_node("A", "formula")])
        thin = analytics.thin_coverage(g, node_type="standard")
        assert thin["total"] == 0
        assert thin["thin"] == []


class TestTraceDependencies:
    @pytest.fixture
    def chain(self):
        return _graph(
            [
                _node("A"),
                _node("B"),
                _node("C"),
                _rel("A", "B"),
                _rel("B", "C"),
            ]
        )

    def test_downstream_is_forward(self, chain):
        t = analytics.trace_dependencies(chain, "A")
        assert t["direct"] == ["B"]
        assert t["reachable"] == 2
        assert {n["node"]: n["depth"] for n in t["nodes"]} == {"B": 1, "C": 2}

    def test_upstream_is_backward(self, chain):
        """方向必须真的反过来 —— 这是最难发现的那类错。"""
        t = analytics.trace_dependencies(chain, "C", direction="upstream")
        assert t["direct"] == ["B"]
        assert {n["node"]: n["depth"] for n in t["nodes"]} == {"B": 1, "A": 2}

    def test_opposite_directions_give_different_answers(self, chain):
        down = analytics.trace_dependencies(chain, "A")["reachable"]
        up = analytics.trace_dependencies(chain, "A", direction="upstream")["reachable"]
        assert down != up, "两个方向给出同一答案 -> 方向参数没生效"

    def test_has_theorem_must_be_read_upstream(self):
        """``has_theorem`` 是 axiom -> theorem, 所以「谁推出 T1」只能反着走。"""
        g = _graph(
            [
                _node("A-1", "axiom"),
                _node("thm::T1", "theorem"),
                _rel("A-1", "thm::T1", "has_theorem"),
            ]
        )
        up = analytics.trace_dependencies(g, "thm::T1", direction="upstream")
        assert up["direct"] == ["A-1"]
        down = analytics.trace_dependencies(g, "thm::T1", direction="downstream")
        assert down["direct"] == [], "顺着走不该有结果 —— 有的话说明边建反了"

    def test_real_seed_theorem_has_an_axiom_upstream(self, seed_graph):
        """实测: thm::T1 由 A-1 推出。顺着走是空的, 反着走才有。"""
        up = analytics.trace_dependencies(seed_graph, "thm::T1", direction="upstream")
        assert up["found"]
        assert up["direct"], "thm::T1 在真实种子里应有上游公理"
        assert up["by_type"].get("axiom", 0) > 0
        down = analytics.trace_dependencies(seed_graph, "thm::T1")
        assert down["reachable"] == 0

    def test_truncation_is_reported(self, chain):
        """截断必须显式报出: 少给几条看起来像「就这些」。"""
        shallow = analytics.trace_dependencies(chain, "A", max_depth=1)
        assert shallow["truncated"] is True
        full = analytics.trace_dependencies(chain, "A", max_depth=5)
        assert full["truncated"] is False
        assert len(full["nodes"]) > len(shallow["nodes"])

    def test_cycle_does_not_hang(self):
        """图里有环时不能无限展开 —— 层次遍历靠 ``layers`` 去重兜底。"""
        g = _graph([_node("A"), _node("B"), _rel("A", "B"), _rel("B", "A")])
        t = analytics.trace_dependencies(g, "A")
        assert t["reachable"] == 1

    def test_cycle_node_keeps_its_shortest_depth(self):
        """环上的节点必须保留**最短**深度。

        这一条比「不得重复」更严也更准: 遍历里 ``layers[n] = depth`` 是**覆盖**
        赋值, 所以去掉去重之后结果**集合仍然正确**(不重复), 只是 depth 被后来的
        更大值覆盖 —— B 明明是 A 的直接后继, 却会显示成 depth 4。只断言「不重复」
        的测试抓不到这种退化, 而调用方正是按 depth 排序看依赖层次的。
        """
        g = _graph(
            [_node("A"), _node("B"), _node("C"), _rel("A", "B"), _rel("B", "C"), _rel("C", "A")]
        )
        t = analytics.trace_dependencies(g, "A", max_depth=5)
        depths = {n["node"]: n["depth"] for n in t["nodes"]}
        assert depths == {"B": 1, "C": 2}, f"环上的 depth 被覆盖成了更远的值: {depths}"
        assert len(depths) == len(t["nodes"]), "同一节点被列了多次"
        assert t["reachable"] == 2

    def test_self_loop_is_dropped_at_build_time(self):
        """自环不建边 (materialize 的既定取舍), 所以不该出现在追溯里。"""
        g = _graph([_node("A"), _rel("A", "A")])
        t = analytics.trace_dependencies(g, "A")
        assert t["reachable"] == 0

    def test_bad_direction_raises(self, chain):
        with pytest.raises(ValueError, match="direction"):
            analytics.trace_dependencies(chain, "A", direction="sideways")

    def test_missing_node_hint_is_actionable(self, chain):
        """「共 N 个节点」没用 —— 人还是不知道该填什么。"""
        t = analytics.trace_dependencies(chain, "thm::T999")
        assert t["found"] is False
        assert "thm::T999" in t["error"]
        assert t["hint"], "找不到节点必须给提示"


class TestMcpTools:
    """MCP 是 §4.4 指定的消费方 —— 产出必须真的到得了调用方手里。"""

    def test_analyze_graph_returns_sparseness_warning(self):
        """稀疏/碎裂必须出现在**每一份** MCP 输出里。

        只在 knowledge_gate 里说一次的话, 通过 MCP 拿到空排名的人无从知道那些 0
        是因为节点没进任何边 —— 而那正是「标准不重要」的错误结论。

        断言的是**规则**而不是当前数字: 警告的有无必须与实测拓扑一致。
        早先这里写死 `assert d["sparseness_warning"]`, 而那时种子孤立率 74% 远高于
        SPARSE_GRAPH_RATIO; 补数据后孤立率降到 50% 以下, 警告按设计消失 —— 旧断言
        于是把「稀疏度已经改善」报成失败。钉死数字等于把数据现状写成契约。

        判据是**两条**(孤立率 / 可达比例), 因为孤立率低不等于图连通: 板卡实测
        孤立 47.47%(恰好不触发), 但 594 节点散成 319 个连通分量、最大分量仅 8.1%。
        """
        import asyncio

        from aterag.mcp_server import server

        d = json.loads(asyncio.run(server.analyze_graph("centrality")))
        assert d["metric"] == "centrality"
        topo = d["topology"]
        assert topo["nodes"] > 0
        note = d["sparseness_warning"]
        assert (note is not None) == _should_warn(topo), (
            "警告有无与实测拓扑不一致: 孤立率 %.1f%%(阈值 %.0f%%), 可达比例 %.1f%%"
            "(阈值 %.0f%%) -> note=%r"
            % (
                topo["isolated_nodes"] / topo["nodes"] * 100,
                server.SPARSE_GRAPH_RATIO * 100,
                topo["largest_component"] / topo["nodes"] * 100,
                server.FRAGMENTED_GRAPH_RATIO * 100,
                note,
            )
        )
        if note:
            # 报出来的必须是**触发的那一条**, 不能拿另一条的原因顶替:
            # 「0 分是因为节点没进任何边」与「追溯只看得到这一块」是两个问题。
            iso = topo["isolated_nodes"] / topo["nodes"] > server.SPARSE_GRAPH_RATIO
            frag = topo["largest_component"] / topo["nodes"] < server.FRAGMENTED_GRAPH_RATIO
            assert ("孤立" in note) == iso, f"孤立判据={iso} 但提示={note!r}"
            assert ("碎的" in note) == frag, f"碎裂判据={frag} 但提示={note!r}"

    def test_trace_dependency_both_directions_reachable(self):
        import asyncio

        from aterag.mcp_server import server

        up = json.loads(asyncio.run(server.trace_dependency("thm::T1", direction="upstream")))
        down = json.loads(asyncio.run(server.trace_dependency("thm::T1")))
        assert up["direction"] == "upstream" and up["direct"]
        assert down["direction"] == "downstream"
        # 断言规则, 不断言「当前一定稀疏」。
        #
        # 期望值必须跟**工具实际用的那张图**对齐 —— ``_kg_graph`` 现在返回合并图
        # (种子 + PG, 实测 841 节点 / 可达 28.8%), 而这里原来是从纯种子(594 /
        # 8.1%)算的拓扑。可达比例跨过 25% 阈值后碎裂判据不再触发, 于是拿纯种子
        # 的拓扑去判合并图的警告, 结论正好反过来 —— 那条断言会去「修」一个没坏的
        # 判据。这里直接用工具返回里的 topology(两个工具现在都给)。
        assert up["topology"]["nodes"] > 0
        assert (up["sparseness_warning"] is not None) == _should_warn(up["topology"]), (
            "追溯输出与它自己报的拓扑不一致"
        )

    def test_unknown_metric_is_an_error_not_empty_success(self):
        import asyncio

        from aterag.mcp_server import server

        d = json.loads(asyncio.run(server.analyze_graph("nope")))
        assert d["error"] == "unknown_metric"
        assert "centrality" in d["hint"]

    def test_bad_direction_is_an_error(self):
        import asyncio

        from aterag.mcp_server import server

        d = json.loads(asyncio.run(server.trace_dependency("thm::T1", direction="sideways")))
        assert d["error"] == "bad_direction"

    def test_missing_node_is_reported_not_raised(self):
        """MCP 工具必须返回结构化错误, 不能把异常抛给协议层。"""
        import asyncio

        from aterag.mcp_server import server

        d = json.loads(asyncio.run(server.trace_dependency("thm::NOPE")))
        assert d["found"] is False
        assert d["hint"]

    def test_analysis_failure_is_an_error_not_empty_success(self, monkeypatch):
        """底层炸了 -> 报 error。

        报成「分析结果为空」是最坏的一种: 调用方会把它当成「这个图没有重要节点」,
        而真相是根本没算成。前者是可行动的结论, 后者让人做出错误决策。
        """
        import asyncio

        from aterag.mcp_server import server

        def boom(*a, **k):
            raise RuntimeError("networkx 崩了")

        monkeypatch.setattr(server, "_kg_graph", boom)
        d = json.loads(asyncio.run(server.analyze_graph("centrality")))
        assert d["error"] == "graph_analysis_failed"
        assert "networkx" in d["message"]

    def test_trace_failure_is_an_error_not_empty_result(self, monkeypatch):
        import asyncio

        from aterag.mcp_server import server

        def boom(*a, **k):
            raise RuntimeError("图文件损坏")

        monkeypatch.setattr(server, "_kg_graph", boom)
        d = json.loads(asyncio.run(server.trace_dependency("thm::T1")))
        assert d["error"] == "trace_failed"
        assert d.get("found") is not True, "失败不得报成 found=True 的空结果"

    def test_unknown_metric_reports_the_supported_one(self):
        """未知 metric 要告诉人支持什么 —— 只说「不支持」等于让人去猜。"""
        import asyncio

        from aterag.mcp_server import server

        d = json.loads(asyncio.run(server.analyze_graph("betweenness")))
        assert d["error"] == "unknown_metric"
        assert "centrality" in d["hint"]


class TestKnowledgeGateIntegration:
    def test_graph_structure_check_is_wired(self):
        import knowledge_gate

        rep = knowledge_gate.GateReport()
        ents, rels = load_seed_records(str(SEED))
        knowledge_gate.check_graph_structure([*ents, *rels], rep)
        assert "graph_nodes" in rep.stats
        assert "graph_edges" in rep.stats
        assert "graph_isolated" in rep.stats

    def test_sparsity_is_warn_not_error(self):
        """稀疏判 WARN: 判 ERROR 会让门禁在种子上永远红。

        永远红的门禁会被整体忽略, 于是真正该看的 ERROR 也一起没人看。
        """
        import knowledge_gate

        rep = knowledge_gate.GateReport()
        ents, rels = load_seed_records(str(SEED))
        knowledge_gate.check_graph_structure([*ents, *rels], rep)
        # 核心不变量: 稀疏永远不判 ERROR。永远红的门禁会被整体忽略。
        assert rep.count("ERROR") == 0, [f.detail for f in rep.findings if f.severity == "ERROR"]
        checks = {f.check for f in rep.findings}
        assert "graph_thin_covered" in checks
        # graph_structure 这条**只在稀疏时**出现 —— 判据同样是规则不是数字。
        # 种子补数据后孤立率已低于阈值, 该 finding 合法消失; 但若哪天又稀疏回去,
        # 它必须回来, 且只能是 WARN。早先写死 `assert "graph_structure" in checks`
        # 把「当时的稀疏度」当成了契约。
        import json as _json

        topo = _json.loads(knowledge_gate.json.dumps(rep.stats))
        isolated = topo["graph_isolated"]
        total = topo["graph_nodes"]
        sparse = isolated / total > 0.5
        if sparse:
            assert "graph_structure" in checks, "稀疏度超阈值时必须报 graph_structure"
        else:
            assert "graph_structure" not in checks, (
                "孤立率 %.1f%% 已低于阈值, 不该再报 graph_structure" % (100.0 * isolated / total)
            )
        for f in rep.findings:
            if f.check == "graph_structure":
                assert f.severity != "ERROR", "稀疏判成了 ERROR"

    def test_unbuildable_graph_is_error_not_skip(self, monkeypatch):
        """图建不起来是真问题 —— 跳过等于把「查不了」报成「没问题」。"""
        import knowledge_gate

        from aterag.kg import analytics

        def boom(_records, **_kw):
            raise RuntimeError("节点字段缺失, 建不了图")

        # knowledge_gate 里是函数内 import, 所以打在被调用的那一份上。
        monkeypatch.setattr(analytics, "graph_from_records", boom)
        rep = knowledge_gate.GateReport()
        knowledge_gate.check_graph_structure([{"id": "A"}], rep)
        assert rep.count("ERROR") > 0
        assert any("图结构检查未完成" in f.detail for f in rep.findings)

    def test_zero_edge_graph_is_error(self):
        """实体都在但一条边都没有 —— 图等于不存在, 判 ERROR, 且要说清成因。"""
        import knowledge_gate

        rep = knowledge_gate.GateReport()
        knowledge_gate.check_graph_structure([_node("A"), _node("B"), _node("C")], rep)
        errs = " ".join(f.detail for f in rep.findings if f.severity == "ERROR")
        assert errs, "零边图没有报 ERROR"
        assert "全部孤立" in errs, errs
        # 要给成因而不只是现象 —— 「一条边都没有」没人知道该查什么
        assert "目标 id" in errs, f"报错没说清成因, 人不知道该查什么: {errs}"

    def test_single_node_is_not_a_structure_error(self):
        """单节点样本不判 ERROR。

        测试夹具与「单条记录重跑」都是单节点, 它们本来就一条边都没有。拿这种输入
        判 ERROR, 门禁就会在最小输入上永远红 —— 而永远红的门禁会被整体忽略,
        于是真正该看的 ERROR 也一起没人看了。
        """
        import knowledge_gate

        rep = knowledge_gate.GateReport()
        knowledge_gate.check_graph_structure([_node("A")], rep)
        errs = [f.detail for f in rep.findings if f.severity == "ERROR"]
        assert not errs, f"单节点被判成结构错误: {errs}"

    def test_two_nodes_no_edge_is_still_error(self):
        """边界: 2 个节点零关系就要判了 —— 门槛不是「2 就放过」。"""
        import knowledge_gate

        rep = knowledge_gate.GateReport()
        knowledge_gate.check_graph_structure([_node("A"), _node("B")], rep)
        assert any("全部孤立" in f.detail for f in rep.findings if f.severity == "ERROR")


def _code_only(path: Path) -> str:
    """去掉注释与文档串后的源码。

    关键字写在 docstring 里是**说明**这个模块不做它, 出现在代码里才是**真的在做**
    —— 所以「不许落盘」「不加时间字段」这两条断言必须扫代码, 不能扫全文。
    """
    import ast

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    drop: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            drop |= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return "\n".join(line for i, line in enumerate(src.splitlines(), 1) if i not in drop)


class TestNoPersistence:
    def test_no_write_back_to_disk_or_db(self):
        """图只在内存 (方案 §4.4「明确不做的」)。

        落盘 = 第二份副本 = 忘了同步就出现「界面显示旧知识、推理用新知识」——
        materialize.py 的 docstring 已把这条教训写死了。
        """
        code = _code_only(ROOT / "src/aterag/kg/analytics.py")
        for forbidden in ("open(", "write_text", "psycopg", "INSERT", "to_file"):
            assert forbidden not in code, f"分析层不该做 {forbidden}"

    def test_no_temporal_fields_added(self):
        """方案「双时态暂不做」: 种子没有时间字段, 加了就是空壳。"""
        code = _code_only(ROOT / "src/aterag/kg/analytics.py")
        assert "valid_from" not in code
        assert "valid_until" not in code
