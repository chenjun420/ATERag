"""图分析层: 把 ContextGraph 适配成 Semantica 组件能吃的形态并产出三样东西 (方案 §4.4)。

为什么需要这一层
----------------
``materialize.build_context_graph`` 产出的是 ``ContextGraph`` (自研 dataclass 图),
而 ``CentralityCalculator`` / ``PathFinder`` / ``GraphValidator`` 吃的是
**NetworkX 图或 ``{"entities": [...], "relationships": [...]}``**。两者没有公共
父类, 所以必须显式转换 —— 不转换就只能让这些组件闲置, 而「组件在依赖里但没人用」
正是 §十 反复出现的形态。

三样产出, 每样都有消费方 (方案 §4.4 的硬要求)
----------------------------------------------
==========================  ====================================
产出                        消费方
==========================  ====================================
:func:`validate_structure`   ``knowledge_gate.py`` 的结构健康项
:func:`centrality_report`    MCP ``analyze_graph(metric="centrality")``
:func:`trace_dependencies`   MCP ``trace_dependency(rule_id)``
==========================  ====================================

**当前种子的实测底图仍极稀疏, 产出信息量有限, 这一点必须如实说**
--------------------------------------------------------
2026-10-08 实测: 594 节点 / 275 边 / 282 个孤立节点(47.5%) / 319 个连通分量 /
最大连通分量 48。仍高于 ``server.py`` 的 ``SPARSE_GRAPH_RATIO = 0.5``, 所以每次
分析都会带 sparseness_warning。早先这里写的「641 节点 / 65 边 / 518 孤立 / 最大
分量 21」是 2026-09 的旧种子, 已漂移 —— **别照抄注释里的数字, 实测
``analytics.topology()``**。孤立率高意味着算出来的集中度与路径多半是 0 或极短
路径。所以本模块的定位是**把能力建好并让产出可见**, 而不是宣称「分析出了很多
洞察」—— 底图要密, 得先补关系(见 ``build_seed_data.py``), 那是另一件事。

不做的 (方案 §4.4「明确不做的」)
--------------------------------
* **不写回 PG**: 图只在内存。落盘 = 第二份副本 = 忘了同步就出现
  「界面显示旧知识、推理用新知识」(materialize.py 的 docstring 已记这条教训)。
* **不加 valid_from/valid_until**: 种子没有任何时间字段, 加了就是空壳。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: 判定「薄覆盖」时用到的分位数。低于 25% 分位即视为「只被少数节点引用」。
THIN_COVERAGE_QUANTILE = 0.25


def _to_networkx(graph: Any) -> Any:
    """ContextGraph -> NetworkX DiGraph。

    **有向图而非无向图**: 关系的方向带语义(``defined_by`` 是「概念被标准定义」,
    反过来读是错的)。用无向图会让「依赖追溯」把方向读反, 而那种错误不会报错,
    只会给出看起来合理的反向答案。
    """
    import networkx as nx

    g = nx.DiGraph()
    for node_id, node in graph.nodes.items():
        g.add_node(node_id, node_type=getattr(node, "node_type", ""))
    for edge in graph.edges:
        g.add_edge(
            edge.source_id,
            edge.target_id,
            edge_type=getattr(edge, "edge_type", "related_to"),
            name=getattr(edge, "edge_type", "related_to"),
        )
    return g


def _from_networkx(g: Any) -> dict[str, Any]:
    """NetworkX -> Semantica 组件吃的 dict 形态。

    ``name`` / ``type`` 必须带上(实体与关系都要): ``GraphValidator`` 要求这两个
    必填字段, 少给就会报出「missing required fields」—— 那是**适配层的错**, 不是图的
    结构问题。一份 700 条全是同一句的 issue 报告, 没人会读第二遍, 于是真正的结构
    问题被彻底淹没。
    """
    return {
        "entities": [
            {
                "id": n,
                "name": n,
                "type": d.get("node_type") or "entity",
            }
            for n, d in g.nodes(data=True)
        ],
        "relationships": [
            {
                "source": s,
                "target": t,
                "name": d.get("edge_type") or "related_to",
                "type": d.get("edge_type") or "related_to",
            }
            for s, t, d in g.edges(data=True)
        ],
    }


def validate_structure(graph: Any, *, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """结构健康报告 (方案 §4.4 的 ``GraphValidator``)。

    ``strict=False``: 种子里有 ``formula`` / ``power_concept`` 这类**本项目自定义**
    的节点类型, 上游 schema 不认识就是不认识。严格模式下每条都会成 issue, 报告
    就退化成「本项目不是上游 schema」这句话 —— 那不是健康报告, 是噪音。

    返回可直接进 ``knowledge_gate`` 的形状 (含 ``issues`` 明细), 而不是把上游
    的 ``ValidationResult`` 直接抛给调用方: 上游对象没有 ``to_dict`` 之外的稳定
    契约, 我们需要在门禁里按自己的严重级分类。
    """
    from semantica.kg import GraphValidator

    g = _to_networkx(graph)
    payload = _from_networkx(g)
    result = GraphValidator(schema=schema, strict=False).validate(payload)

    issues: list[dict[str, Any]] = []
    for issue in result.issues or []:
        issues.append(
            {
                "severity": str(getattr(issue, "severity", "")),
                "message": str(getattr(issue, "message", issue)),
                "node": str(getattr(issue, "node_id", "") or getattr(issue, "entity_id", "")),
            }
        )
    stats = dict(result.stats or {})
    stats.update(_topology_stats(g))
    return {
        "is_valid": bool(result.is_valid),
        "issues": issues,
        "stats": stats,
        "topology": _topology_stats(g),
    }


def topology(graph: Any) -> dict[str, Any]:
    """图的拓扑关键数字 —— 「有没有退化」的直接证据。

    公开给消费方(MCP 工具在返回结果里带上它), 因为稀疏度这件事必须出现在**每一份**
    分析输出里: 只在 ``knowledge_gate`` 里说一次的话, 通过 MCP 拿到空排名的人
    无从知道那些 0 是因为节点没进任何边。
    """
    return _topology_stats(_to_networkx(graph))


def _topology_stats(g: Any) -> dict[str, Any]:
    """图拓扑的关键数字 —— 这些是「有没有退化」的直接证据。

    放进报告而不是 print: 门禁要用它判 ERROR/WARN, 而 print 出来的数字无法被断言。
    """
    import networkx as nx

    nodes = g.number_of_nodes()
    edges = g.number_of_edges()
    weak = list(nx.weakly_connected_components(g))
    isolated = [n for n in g.nodes if g.degree(n) == 0]
    return {
        "nodes": nodes,
        "edges": edges,
        "isolated_nodes": len(isolated),
        "components": len(weak),
        "largest_component": max((len(c) for c in weak), default=0),
        "nodes_in_edges": nodes - len(isolated),
    }


def centrality_report(
    graph: Any, *, metrics: Iterable[str] = ("degree", "pagerank")
) -> dict[str, Any]:
    """中心性分析 (方案 §4.4 优先项)。

    目标是「哪些标准只被 1 个概念引用」= 覆盖薄弱行动项, 所以除了排名, 还直接
    给出 ``thin_covered`` 清单 —— 排名要人自己找阈值, 行动项不该再让人找一次。
    """
    from semantica.kg import CentralityCalculator

    g = _to_networkx(graph)
    calc = CentralityCalculator()
    out: dict[str, Any] = {"metrics": {}, "top": {}, "thin_covered": []}

    scores: dict[str, dict[str, float]] = {}
    for metric in metrics:
        try:
            fn = getattr(calc, f"calculate_{metric}_centrality", None) or (
                calc.calculate_pagerank if metric == "pagerank" else None
            )
            if fn is None:
                logger.warning("中央性指标 %s 不受支持, 跳过", metric)
                continue
            # PageRank 默认 20 次迭代收敛不了本项目的稀疏图 (实测报
            # "did not converge") —— 大量孤立节点会让衰减因子迟迟不达标。
            # 给足迭代再拿结果, 而不是退回 degree: 那会让「排名」变成另一种度量。
            if metric == "pagerank":
                res = calc.calculate_pagerank(_from_networkx(g), max_iterations=200)
            else:
                res = fn(_from_networkx(g))
        except Exception as e:  # noqa: BLE001 单个指标失败不该让整份报告没有
            logger.warning("中央性指标 %s 计算失败: %s", metric, e)
            out["metrics"][metric] = {"error": f"{type(e).__name__}: {e}"}
            continue
        vals = _extract_scores(res)
        scores[metric] = vals
        out["metrics"][metric] = {"scored_nodes": len(vals)}
        top = sorted(vals.items(), key=lambda kv: -kv[1])[:20]
        out["top"][metric] = [{"node": n, "score": round(s, 6)} for n, s in top]

    out["thin_covered"] = thin_coverage(graph)
    return out


def _extract_scores(res: Any) -> dict[str, float]:
    """从组件返回值里取 ``{node: score}``, 兼容几种返回形态。

    上游不同方法的返回形状不统一(有的直接给 dict, 有的裹在 ``centrality`` /
    ``scores`` 里), 所以这里逐个认。认不出来就返回空 —— 让调用方看到「没算出来」,
    而不是拿到一个形状对但内容错的 dict。
    """
    if isinstance(res, dict):
        for key in ("centrality", "scores", "pagerank"):
            inner = res.get(key)
            if isinstance(inner, dict):
                return {str(k): float(v) for k, v in inner.items() if _is_num(v)}
        if res and all(_is_num(v) for v in res.values()):
            return {str(k): float(v) for k, v in res.items()}
    return {}


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def thin_coverage(graph: Any, *, node_type: str = "standard") -> dict[str, Any]:
    """被引用次数在薄尾部的节点 (方案 §4.4 的行动项)。

    阈值取**分位数**而不是写死「只被 1 个引用」: 节点数变化时固定阈值要么全中要么
    全不中, 而这两种都等于没报。
    """
    g = _to_networkx(graph)
    targets = [n for n, d in g.nodes(data=True) if d.get("node_type") == node_type]
    if not targets:
        return {"node_type": node_type, "count": 0, "thin": [], "threshold": 0.0, "total": 0}

    indeg: dict[str, int] = {}
    for t in targets:
        indeg[t] = g.in_degree(t)
    ordered = sorted(indeg.items(), key=lambda kv: kv[1])
    idx = max(0, int(len(ordered) * THIN_COVERAGE_QUANTILE) - 1)
    threshold = ordered[idx][1] if ordered else 0
    thin = [{"node": n, "in_degree": c} for n, c in ordered if c <= threshold]
    return {
        "node_type": node_type,
        "count": len(thin),
        "total": len(targets),
        "threshold": threshold,
        "zero_in_degree": sum(1 for _, c in ordered if c == 0),
        "thin": thin[:50],
    }


def trace_dependencies(
    graph: Any, node_id: str, *, max_depth: int = 5, direction: str = "downstream"
) -> dict[str, Any]:
    """从某节点出发能到达什么 (方案 §4.4 的依赖追溯)。

    用 NetworkX 的可达性而不是 ``PathFinder``: 后者给的是**最短路径**, 而
    「K-ELEC-001 依赖哪些上游」要的是**集合**。给集合用最短路径实现会在多路径时
    漏掉分支, 而漏掉的分支在图上完全看不出异常。

    ``direction`` 必须显式给, 因为两个方向回答的是**不同的问题**
    ------------------------------------------------------
    - ``downstream``: 我引用了谁 —— 顺着 ``defined_by`` 读作「这个概念依据哪份标准」。
    - ``upstream``: 谁引用了我 —— 在 ``has_theorem`` 这条边上, 问「thm::T1 是被哪条
      公理推出来的」**必须反着走**。顺着走会得到「T1 不依赖任何人」, 而实际有
      公理指向它。方向读反不报错, 只给出一个看起来合理的空答案。

    不给默认值之外的推断: ``downstream`` 是默认, 但 ``upstream`` 才是 ``has_theorem``
    边上唯一有意义的读法, 靠调用方记得传对参数是不可靠的。
    """
    if direction not in {"downstream", "upstream"}:
        raise ValueError(f"direction 只能是 downstream / upstream, 实际 {direction!r}")

    g = _to_networkx(graph)
    if node_id not in g:
        return {
            "node": node_id,
            "found": False,
            "direction": direction,
            "error": f"节点不在图里: {node_id}",
            "hint": _id_hint(g, node_id),
        }

    step = g.successors if direction == "downstream" else g.predecessors
    full_depth = _full_depth(g, node_id, direction=direction)

    layers: dict[str, int] = {node_id: 0}
    frontier = {node_id}
    for depth in range(1, max_depth + 1):
        nxt: set[str] = set()
        for n in frontier:
            nxt |= set(step(n))
        nxt -= layers.keys()
        if not nxt:
            break
        for n in nxt:
            layers[n] = depth
        frontier = nxt

    reachable = {n: d for n, d in layers.items() if n != node_id}
    return {
        "node": node_id,
        "found": True,
        "direction": direction,
        "direct": sorted(step(node_id)),
        "reachable": len(reachable),
        "max_depth_reached": max(reachable.values()) if reachable else 0,
        "by_type": _type_histogram(g, reachable),
        "truncated": max_depth < full_depth,
        "full_depth": full_depth,
        "nodes": [
            {"node": n, "depth": d} for n, d in sorted(reachable.items(), key=lambda kv: kv[1])
        ],
    }


def _id_hint(g: Any, node_id: str) -> str:
    """找不到节点时给**可操作**的提示。

    「图里共 641 个节点」这种提示没用 —— 人还是不知道该填什么。所以给节点最多的
    类型, 以及同前缀下真实存在的 id(``thm::T1`` 拼错成 ``theorem::T1`` 时,
    看到 ``thm::`` 下真实的那批就够纠正了)。
    """
    all_ids = sorted(g.nodes)
    types: dict[str, list[str]] = {}
    for n, d in g.nodes(data=True):
        types.setdefault(d.get("node_type") or "entity", []).append(n)
    biggest = max(types.items(), key=lambda kv: len(kv[1]))[0] if types else "?"
    ns = node_id.split("::", 1)[0] + "::" if "::" in node_id else ""
    same_ns = sorted(n for n in all_ids if n.startswith(ns)) if ns else []
    hint = f"图里共 {len(all_ids)} 个节点; 节点最多的类型是 {biggest}({len(types.get(biggest, []))} 个)。"
    if same_ns:
        return hint + f" 同前缀 {ns!r} 下真实存在: {same_ns[:8]}"
    return hint + f" 例如 {all_ids[:8]}"


def _full_depth(g: Any, node_id: str, *, direction: str = "downstream") -> int:
    """全深度 —— 用于判断 ``max_depth`` 是否截断了结果。

    截断必须**显式报出**: 少给几条看起来像「就这些」, 而那是最容易被当成结论的
    那种不完整。
    """
    import networkx as nx

    try:
        view = g if direction == "downstream" else g.reverse(copy=False)
        return nx.dag_longest_path_length(view, node_id) if g.number_of_edges() else 0
    except Exception:  # noqa: BLE001 有环等异常结构下取不到, 按「没截断」处理
        return 0


def _type_histogram(g: Any, nodes: Iterable[str]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for n in nodes:
        t = g.nodes[n].get("node_type", "?") if n in g else "?"
        hist[t] = hist.get(t, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: -kv[1]))


def graph_from_records(
    records: Iterable[Mapping[str, Any]], *, advanced_analytics: bool = False
) -> Any:
    """便捷入口: 直接从扁平种子记录建图。

    存在的理由: 三处调用方(analytics 自身测试 / MCP 工具 / 脚本)都需要
    「记录 -> 图」这一步, 各写一遍就会各自漂 —— 而漂掉的那份通常漏掉取舍逻辑,
    于是同一份种子在两处得出不同的图。
    """
    from aterag.kg.materialize import build_context_graph

    graph, n_nodes, n_edges = build_context_graph(
        list(records), [], advanced_analytics=advanced_analytics
    )
    logger.info("分析用图: %d 节点 / %d 边", n_nodes, n_edges)
    return graph
