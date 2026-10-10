#!/usr/bin/env python3
"""PA601-D54A 实测基准 × semantica 能力矩阵 —— 可复跑的验收脚本。

为什么要有这个脚本
------------------
semantica 的能力「能import」与「在服务里可用」是两回事, 而浅层探测会得出
反向结论。已实测确认的三处:

1. ``ApacheAgeStore.connect()`` 对服务角色 ``powerspec`` **必然失败**
   (``LOAD 'age'`` 要超级用户)。只试它会得出「AGE 适配器不可用」——
   而 ``scripts/sync_semantica.py:135`` 的 ``_AgeStoreNoLoad`` 已经绕开了,
   AGE 图实测 426 节点 / 520 边。
2. 手写 ``SELECT ... FROM cypher(...)`` 三种语法全被 AGE 拒绝
   (「一个字段定义列表需要返回 record 的函数」), **只能经 shim 的
   ``execute_query`` 访问**。所以「AGE 里没数据」也是个错误结论。
3. ``GraphValidator.validate()`` 收 ``{"entities", "relationships"}``;
   给 ``{"nodes", "edges"}`` 它读到 0 个实体却照样 ``is_valid=True`` ——
   违规图也判通过。拿直觉键名写断言会得到一个「零工作量通过」。

断言基准全部取自可核对的实物, 不写「能import 就算过」:

  54V x 11.1A = 599.4W        domain_rules/power/rules.yaml K-ELEC-001
  半载 = 599.4 x 0.5 = 299.7W 种子 facts load-ratio-half_load
  种子 869 条 = 594 实体 + 275 关系
  rules.yaml 130 条规则 = AGE 里 Rule 标签 130 个
  PA601 实体 242 条 / 全库实体 247 条 / 分块 242 条

跑法::

    cd /opt/aterag && sudo -u aterag .venv/bin/python scripts/verify_semantica_caps.py
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import sys
import traceback
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEED_PATH = ROOT / "data" / "seed" / "power_domain_seed.json"

MODEL = "PA601-D54A"
FULL_LOAD_W = 599.4  # 54V x 11.1A
HALF_LOAD_W = 299.7  # FULL_LOAD_W x 0.5
RULES_TOTAL = 130  # rules.yaml 条数 == AGE Rule 标签数
SEED_ENTITIES = 594
SEED_RELATIONS = 275

RESULTS: list[tuple[str, str, bool, str]] = []


def check(cap: str, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((cap, name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} [{cap}] {name}" + (f"  | {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'-' * 76}\n{title}\n{'-' * 76}")


def asdict(obj: object) -> dict:
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    return dict(getattr(obj, "__dict__", {}))


def main() -> int:  # noqa: PLR0912, PLR0915
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(ROOT / "src"))

    import psycopg

    from aterag.config import Settings

    s = Settings()
    dsn = s.postgres_dsn
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ 1
    section("1  KnowledgeGraph -- 图谱数据载体")
    try:
        from semantica.kg import KnowledgeGraph

        ents = [r for r in seed["records"] if r.get("entity_type")]
        rels = [r for r in seed["records"] if not r.get("entity_type")]
        check("KnowledgeGraph", "种子实体数 = 594", len(ents) == SEED_ENTITIES, f"{len(ents)}")
        check("KnowledgeGraph", "种子关系数 = 275", len(rels) == SEED_RELATIONS, f"{len(rels)}")
        kg = KnowledgeGraph(entities=ents, relationships=rels)
        check(
            "KnowledgeGraph",
            "承载实体与关系",
            len(kg.entities) == SEED_ENTITIES and len(kg.relationships) == SEED_RELATIONS,
            f"{len(kg.entities)} 实体 / {len(kg.relationships)} 关系",
        )
        types = {e["entity_type"] for e in ents}
        check(
            "KnowledgeGraph",
            "领域类型齐全(formula/power_concept/symbol/standard)",
            {"formula", "power_concept", "symbol", "standard"} <= types,
            f"{len(types)} 类",
        )
        # 反向: 空图必须真的是空, 不能残留上一次的量
        empty = KnowledgeGraph()
        check(
            "KnowledgeGraph",
            "反向: 默认构造为空",
            empty.entities == [] and empty.relationships == [],
            "entities=[] relationships=[]",
        )
        print(
            f"NOTE  KnowledgeGraph 持有入参列表引用(kg.entities is ents = "
            f"{kg.entities is ents}); 生产路径只读, 暂不致命, 改动前需留意"
        )
    except Exception as e:  # noqa: BLE001
        check("KnowledgeGraph", "承载", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # ------------------------------------------------------------ 2
    section("2  GraphValidator -- 形状校验(必须用 entities/relationships 键)")
    try:
        from semantica.kg import GraphValidator

        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                "SELECT eid, etype FROM aterag_entities WHERE model_id=%s", (MODEL,)
            ).fetchall()
        good = [{"id": e, "type": t, "name": e, "properties": {}} for e, t in rows]
        check("GraphValidator", "PA601 实体已入库", len(good) > 0, f"{len(good)} 条")

        v = GraphValidator(
            schema={
                "node_types": sorted({t for _e, t in rows}),
                "relationship_types": ["has", "defined_by", "BELONGS_TO", "CITES"],
            },
            strict=False,
        )

        right = v.validate({"entities": good, "relationships": []})
        check(
            "GraphValidator",
            "entities/relationships 键: 真正读到实体",
            right.stats.get("total_entities") == len(good),
            f"total_entities={right.stats.get('total_entities')} 期望 {len(good)}",
        )

        # 反向: 换错键必须**看不出来**才是危险 —— 这条把已知陷阱钉住
        wrong = v.validate({"nodes": good, "edges": []})
        check(
            "GraphValidator",
            "已知陷阱: nodes/edges 键读到 0 个实体却报 is_valid=True",
            wrong.stats.get("total_entities") == 0 and wrong.is_valid is True,
            f"total_entities={wrong.stats.get('total_entities')} is_valid={wrong.is_valid} "
            f"—— 所以断言必须用 entities/relationships, 否则是假通过",
        )

        bad = v.validate(
            {"entities": good + [{"id": "BAD-1", "name": "缺 type"}], "relationships": []}
        )
        check(
            "GraphValidator",
            "反向: 缺字段实体被判违规(否则校验器恒真)",
            bad.is_valid is False or len(bad.issues) > len(right.issues),
            f"合规图 {right.is_valid}/{len(right.issues)}条 -> 违规图 {bad.is_valid}/{len(bad.issues)}条",
        )
    except Exception as e:  # noqa: BLE001
        check("GraphValidator", "校验", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # ------------------------------------------------------------ 3
    section("3  ApacheAgeStore -- 官方 API 不可用, 经 _AgeStoreNoLoad 可用")
    try:
        from semantica.graph_store import ApacheAgeStore

        store = ApacheAgeStore(connection_string=dsn, graph_name=s.semantica_graph)
        official_ok = False
        official_err = ""
        try:
            official_ok = bool(store.connect())
        except Exception as e:  # noqa: BLE001
            official_err = f"{type(e).__name__}: {str(e)[:70]}"

        sys.path.insert(0, str(ROOT / "scripts"))
        from sync_semantica import _AgeStoreNoLoad

        shim = _AgeStoreNoLoad.build(dsn, s.semantica_graph)
        shim_ok = bool(shim.connect())
        check("ApacheAgeStore", "_AgeStoreNoLoad.connect()", shim_ok, "shim 连接成功")
        # 把「官方不可用 + shim 可用」当成**一对**已知条件断言:
        # 板卡上 powerspec 非超级用户, LOAD 'age' 必被拒, 官方 API 走不通。
        # 若哪天这条变成 official_ok=True, 说明权限模型变了, 应当重新评估
        # sync_semantica.py 那层绕行是否还有必要 —— 所以它必须显式钉住,
        # 而不是悄悄判 FAIL 被忽略。
        check(
            "ApacheAgeStore",
            "已知条件: 官方 API 不可用但 shim 可用(权限模型未变)",
            (not official_ok) and shim_ok,
            f"official_ok={official_ok} {official_err} / shim_ok={shim_ok}",
        )
        st = shim.get_stats()
        check(
            "ApacheAgeStore",
            "AGE 图非空(实测 426 节点 / 520 边)",
            st.get("node_count", 0) > 0 and st.get("relationship_count", 0) > 0,
            f"{st.get('node_count')} 节点 / {st.get('relationship_count')} 边",
        )
        labels = st.get("label_counts") or {}
        check(
            "ApacheAgeStore",
            f"Rule 标签数 = rules.yaml 的 {RULES_TOTAL}",
            labels.get("Rule") == RULES_TOTAL,
            f"Rule={labels.get('Rule')} 全标签={labels}",
        )
        rel_types = st.get("relationship_type_counts") or {}
        check(
            "ApacheAgeStore",
            "SHACL 约束已建成边(Shape=77)",
            labels.get("Shape") == 77 and rel_types.get("HAS_CONSTRAINT") == 77,
            f"Shape={labels.get('Shape')} HAS_CONSTRAINT={rel_types.get('HAS_CONSTRAINT')}",
        )
        q = shim.execute_query("MATCH (n:Rule) RETURN n.key LIMIT 1")
        check(
            "ApacheAgeStore",
            "execute_query 可取回规则节点",
            bool(q.get("success")) and bool(q.get("records")),
            f"{str(q.get('records'))[:110]}",
        )
        shim.close()

        refs = [
            str(p.relative_to(ROOT))
            for p in (ROOT / "src").rglob("*.py")
            if "ApacheAgeStore" in p.read_text(encoding="utf-8", errors="replace")
            or "cypher(" in p.read_text(encoding="utf-8", errors="replace")
        ]
        check(
            "ApacheAgeStore",
            "生产 src/ 零消费(AGE 只写不读)",
            not refs,
            f"src 命中 {refs}" if refs else "写入在 sync_semantica.py; 服务读的是 PG 表",
        )
    except Exception as e:  # noqa: BLE001
        check("ApacheAgeStore", "AGE 适配", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # ------------------------------------------------------------ 4
    section("4  ProvenanceManager -- 来源谱系(落文件, 不落 PG)")
    try:
        from semantica.provenance import ProvenanceManager

        with psycopg.connect(dsn) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
                ).fetchall()
            ]
        prov_tbl = [t for t in tables if any(k in t for k in ("proven", "lineage", "audit"))]
        check(
            "ProvenanceManager",
            "PG 里没有谱系表(谱系走 JSON 文件)",
            not prov_tbl,
            f"public 共 {len(tables)} 张: {tables}",
        )

        pm = ProvenanceManager(storage_path="/tmp/verify_prov.json")
        ent = next(e for e in seed["records"] if e.get("entity_type") == "standard")
        pm.track_entity(
            entity_id=ent["id"], source=ent.get("source") or "seed", metadata={"model": MODEL}
        )
        got = pm.get_provenance(ent["id"])
        check(
            "ProvenanceManager",
            "能写入并回查来源",
            bool(got) and got.get("entity_id") == ent["id"],
            f"{ent['id']} -> {str(got)[:110]}",
        )
        chk = pm.check()
        check(
            "ProvenanceManager",
            "自检 valid",
            chk.get("valid") is True and chk.get("total_entries", 0) >= 1,
            f"{chk}",
        )
    except Exception as e:  # noqa: BLE001
        check("ProvenanceManager", "谱系", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # ------------------------------------------------------------ 5
    section("5  ContextGraph -- 上下文图(高级分析需 gensim, 板卡未装)")
    try:
        import importlib.metadata as md

        from semantica.context import ContextGraph

        try:
            md.version("gensim")
            gensim = True
        except md.PackageNotFoundError:
            gensim = False

        cg = ContextGraph()
        cg.add_node(MODEL, "model", content="定制电源整机")
        cg.add_node("SR-1204", "requirement", content=f"输出功率 {FULL_LOAD_W}W")
        cg.add_node("K-ELEC-001", "rule", content="功率 = 电压 x 电流")
        cg.add_edge(MODEL, "SR-1204", "has_requirement")
        cg.add_edge("SR-1204", "K-ELEC-001", "verified_by")
        check(
            "ContextGraph",
            "PA601 需求链可建",
            cg.has_node("SR-1204") and cg.has_node("K-ELEC-001"),
            "整机 -> SR-1204 -> K-ELEC-001",
        )
        ids = {(n.get("id") or n.get("node_id")) for n in cg.get_neighbors(MODEL, hops=2)}
        check("ContextGraph", "两跳可达 K-ELEC-001", "K-ELEC-001" in ids, f"{sorted(ids)}")
        stats = cg.stats()
        check(
            "ContextGraph",
            "stats 报出节点/边/类型",
            stats.get("node_count") == 3 and stats.get("edge_count") == 2,
            f"{stats}",
        )
        conn_res = cg.analyze_connections()
        err = conn_res.get("error") if isinstance(conn_res, dict) else None
        check(
            "ContextGraph",
            "analyze_connections 可用"
            if gensim
            else "analyze_connections 不可用(缺 gensim, 属已知降级)",
            (err is None) if gensim else bool(err),
            f"error={err}" if err else "无错误",
        )
        d = cg.to_dict()
        check(
            "ContextGraph",
            "可导出供 GraphSession 用",
            "nodes" in d and "edges" in d,
            f"keys={sorted(d)}",
        )
    except Exception as e:  # noqa: BLE001
        check("ContextGraph", "上下文图", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # ------------------------------------------------------------ 6
    section("6  GraphSession -- Explorer 会话(与 /aterag/graph/summary 同一条路径)")
    try:
        from semantica.explorer.session import GraphSession

        from aterag.kg import graph as kg_graph

        g, _stats = kg_graph.build_graph(dsn, model_ids=None)
        sess = GraphSession(g)
        st = sess.get_stats()
        check(
            "GraphSession",
            "包装 build_graph 产物",
            st.get("node_count", 0) > 0 and st.get("edge_count", 0) > 0,
            f"{st.get('node_count')} 节点 / {st.get('edge_count')} 边",
        )
        nodes, total = sess.get_nodes(limit=5)
        check("GraphSession", "分页取节点", len(nodes) > 0, f"{len(nodes)}/{total}")
        reqs, rtot = sess.get_nodes(node_type="model/Requirement", limit=5)
        check("GraphSession", "按 node_type 过滤出需求", len(reqs) > 0, f"{len(reqs)}/{rtot}")
        hits = sess.search("输出功率", limit=10)
        check("GraphSession", "检索命中输出功率", len(hits) > 0, f"{len(hits)} 条命中")
        edges, etot = sess.get_edges(limit=5)
        check("GraphSession", "分页取边", len(edges) > 0, f"{len(edges)}/{etot}")
        if nodes:
            nb = sess.get_neighbors(nodes[0]["id"], depth=1)
            check("GraphSession", "邻域展开", isinstance(nb, list), f"{len(nb)} 个邻居")
    except Exception as e:  # noqa: BLE001
        check("GraphSession", "会话", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # ------------------------------------------------------------ 7
    section("7  SourceTracker + ConflictResolver -- 来源冲突消解")
    try:
        from semantica.conflicts.conflict_detector import (
            Conflict,
            ConflictType,
            SourceTracker,
        )
        from semantica.conflicts.conflict_resolver import (
            ConflictResolver,
            ResolutionStrategy,
        )

        st = SourceTracker()
        st.register_source("spec.md", "spec", credibility_score=1.0)
        st.register_source("method.md", "method", credibility_score=0.6)
        check(
            "SourceTracker",
            "来源可信度可登记可查",
            st.get_source_credibility("spec.md") == 1.0,
            f"spec={st.get_source_credibility('spec.md')} "
            f"method={st.get_source_credibility('method.md')}",
        )

        from semantica.conflicts.source_tracker import SourceReference

        st.track_property_source(
            MODEL, "output_power", FULL_LOAD_W, SourceReference(document="spec.md", confidence=0.95)
        )
        st.track_property_source(
            MODEL, "output_power", ALT_VALUE, SourceReference(document="method.md", confidence=0.6)
        )
        dis = st.find_source_disagreements(MODEL, "output_power")
        check("SourceTracker", "检出 output_power 分歧", len(dis) > 0, f"{len(dis)} 条")
        check(
            "SourceTracker",
            "可追溯链非空",
            len(st.get_traceability_chain(MODEL, "output_power")) > 0,
            "",
        )

        def resolve(cid, conf_a, conf_b, values=(FULL_LOAD_W, ALT_VALUE), creds=None):
            sa = {"document": "spec.md", "source_id": "s", "confidence": conf_a}
            sb = {"document": "method.md", "source_id": "m", "confidence": conf_b}
            if creds:
                # 故意写进 sources, 用来证明上游不读它
                sa["credibility_score"] = creds[0]
                sb["credibility_score"] = creds[1]
            c = Conflict(
                conflict_id=cid,
                conflict_type=ConflictType.VALUE_CONFLICT,
                entity_id=MODEL,
                property_name="output_power",
                conflicting_values=list(values),
                sources=[sa, sb],
            )
            r = ConflictResolver(default_strategy=ResolutionStrategy.CREDIBILITY_WEIGHTED.value)
            r.set_source_tracker(st)
            return asdict(r.resolve_conflict(c))

        # 权重模型是**乘积**: confidence x (tracker 里按 document 查到的 credibility)。
        # 我第一版这里断言成「confidence 高者胜」, 是错的 —— 规格书 cred 1.0、
        # 方法 cred 0.6 时, 方法即使 confidence 更高(0.9 x 0.6 = 0.54 <
        # 0.3 x 1.0 之外的 0.9 x 1.0)也该输。断言必须按乘积算, 否则测的是
        # 另一个模型, 绿了也说明不了问题。
        spec_w = lambda c: c * 1.0  # noqa: E731
        meth_w = lambda c: c * 0.6  # noqa: E731

        a = resolve("a", 0.9, 0.6)
        check(
            "ConflictResolver",
            "乘积模型: 高 confidence + 高 credibility 的一方胜",
            a.get("resolved_value") == FULL_LOAD_W and spec_w(0.9) > meth_w(0.6),
            f"conf(0.9,0.6) 权重 spec={spec_w(0.9):.2f} method={meth_w(0.6):.2f} "
            f"-> {a.get('resolved_value')}",
        )

        b = resolve("b", 0.3, 0.9)
        check(
            "ConflictResolver",
            "乘积模型: confidence 差得够多时低 credibility 一方也能翻盘",
            b.get("resolved_value") == ALT_VALUE and meth_w(0.9) > spec_w(0.3),
            f"conf(0.3,0.9) 权重 spec={spec_w(0.3):.2f} method={meth_w(0.9):.2f} "
            f"-> {b.get('resolved_value')}",
        )

        c2 = resolve("c", 0.6, 0.9)
        check(
            "ConflictResolver",
            "反向: 乘积接近时高 credibility 方仍胜(conf 0.9x0.6=0.54 < 0.6x1.0=0.6)",
            c2.get("resolved_value") == FULL_LOAD_W,
            f"conf(0.6,0.9) -> {c2.get('resolved_value')} "
            f"(若这条红了, 说明上游已改成只看 confidence)",
        )

        print(
            "NOTE  credibility 取自 SourceTracker 注册表(按 document 查), "
            "不是 conflict.sources[].credibility_score —— 实测后者完全无效"
        )
        check(
            "ConflictResolver",
            "credibility_score 写进 sources 无效(钉住这个陷阱)",
            resolve("d", 0.6, 0.9, creds=(0.0, 1.0)).get("resolved_value")
            == resolve("d2", 0.6, 0.9).get("resolved_value"),
            "把 spec 的 credibility_score 改成 0.0 结论不变 -> 上游只认 tracker 注册表",
        )

        hist_r = ConflictResolver(default_strategy=ResolutionStrategy.CREDIBILITY_WEIGHTED.value)
        hist_r.set_source_tracker(st)
        hist_r.resolve_conflict(
            Conflict(
                conflict_id="hist",
                conflict_type=ConflictType.VALUE_CONFLICT,
                entity_id=MODEL,
                property_name="output_power",
                conflicting_values=[FULL_LOAD_W, ALT_VALUE],
                sources=[
                    {"document": "spec.md", "source_id": "s", "confidence": 0.95},
                    {"document": "method.md", "source_id": "m", "confidence": 0.6},
                ],
            )
        )
        hist = hist_r.get_resolution_history()
        check(
            "ConflictResolver",
            "消解历史可回查",
            len(hist) > 0,
            f"{len(hist)} 条; 首条 conflict_id="
            f"{asdict(hist[0]).get('conflict_id') if hist else '无'}",
        )
    except Exception as e:  # noqa: BLE001
        check("ConflictResolver", "冲突消解", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    section("7b  CredibilityOnlyConflictResolver -- 生产唯一入口")
    try:
        from aterag.conflicts.adapter import (
            CREDIBILITY_BY_AUTHORITY,
            CredibilityOnlyConflictResolver,
            make_conflict,
        )

        cr = CredibilityOnlyConflictResolver()
        cr.register_source(SPEC_DOC, authority_kind="spec")
        cr.register_source(METHOD_DOC, authority_kind="industry")
        check(
            "Adapter",
            "分级表可登记",
            cr.credibility_of(SPEC_DOC) == CREDIBILITY_BY_AUTHORITY["spec"],
            f"spec={cr.credibility_of(SPEC_DOC)} industry={cr.credibility_of(METHOD_DOC)}",
        )

        s_spec = {"document": SPEC_DOC, "source_id": "s", "confidence": 0.95}
        s_meth = {"document": METHOD_DOC, "source_id": "m", "confidence": 0.6}
        r1 = asdict(
            cr.resolve(
                make_conflict(MODEL, "output_power", [FULL_LOAD_W, ALT_VALUE], [s_spec, s_meth])
            )
        )
        check(
            "Adapter",
            "采信可信度更高的来源",
            r1.get("resolved_value") == FULL_LOAD_W,
            f"-> {r1.get('resolved_value')} notes={str(r1.get('resolution_notes'))[:70]}",
        )

        r2 = asdict(
            cr.resolve(
                make_conflict(
                    MODEL,
                    "output_power",
                    [FULL_LOAD_W, ALT_VALUE],
                    [{"document": "未登记.md", "source_id": "u", "confidence": 0.9}],
                )
            )
        )
        check(
            "Adapter",
            "反向: 未登记来源转人审",
            r2.get("resolved") is False,
            f"notes={str(r2.get('resolution_notes'))[:80]}",
        )

        r3 = asdict(
            cr.resolve(
                make_conflict(
                    MODEL,
                    "output_power",
                    [FULL_LOAD_W, ALT_VALUE],
                    [{"document": SPEC_DOC, "source_id": "s", "confidence": None}],
                )
            )
        )
        check(
            "Adapter",
            "反向: 缺 confidence 转人审",
            r3.get("resolved") is False,
            f"notes={str(r3.get('resolution_notes'))[:80]}",
        )

        try:
            cr.resolve(
                make_conflict(MODEL, "output_power", [FULL_LOAD_W, ALT_VALUE], [s_spec, s_meth]),
                strategy="voting",
            )
            check("Adapter", "反向: 拒绝换策略(红线)", False, "竟然接受了 strategy='voting'")
        except ValueError:
            check("Adapter", "反向: 拒绝换策略(红线)", True, "ValueError")
    except Exception as e:  # noqa: BLE001
        check("Adapter", "生产消解入口", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # ------------------------------------------------------------ 8
    section("8  DatalogReasoner -- 种子规则/事实端到端(引擎无算术是设计)")
    try:
        from semantica.reasoning.datalog_reasoner import DatalogReasoner

        from aterag.reasoning.load_scaling import scale, scale_bindings

        r = DatalogReasoner()
        nf = nr = 0
        for f in seed["facts"]:
            try:
                r.add_fact(f["fact_str"])
                nf += 1
            except Exception as e:  # noqa: BLE001
                print(f"NOTE  事实 {f['fact_id']} 被拒: {type(e).__name__}: {str(e)[:70]}")
        for rule in seed["rules"]:
            try:
                r.add_rule(rule["rule_str"])
                nr += 1
            except Exception as e:  # noqa: BLE001
                print(f"NOTE  规则 {rule['rule_id']} 被拒: {type(e).__name__}: {str(e)[:70]}")
        check(
            "DatalogReasoner",
            f"种子 {len(seed['facts'])} 条事实全部写入",
            nf == len(seed["facts"]),
            f"{nf}/{len(seed['facts'])}",
        )
        check(
            "DatalogReasoner",
            f"种子 {len(seed['rules'])} 条规则全部写入",
            nr == len(seed["rules"]),
            f"{nr}/{len(seed['rules'])}",
        )

        pairs = {(str(x.get("X")), str(x.get("Y"))) for x in r.query("load_ratio(X, Y)")}
        check("DatalogReasoner", "推出 3 个工况比例", len(pairs) >= 3, f"{sorted(pairs)}")
        check(
            "DatalogReasoner",
            "别名规则生效: 50pct_load 归一到 0.5",
            any(a == "50pct_load" and abs(float(b) - 0.5) < 1e-9 for a, b in pairs),
            f"{sorted(pairs)}",
        )

        r.add_fact(f"value_at_full_load(output_power, {FULL_LOAD_W})")
        binds = r.query("value_at_load(output_power, L, F, Rt)")
        check(
            "DatalogReasoner", f"推出满载 {FULL_LOAD_W}W 的四元组", len(binds) > 0, f"{binds[:2]}"
        )

        scaled = scale_bindings(
            binds, quantity="output_power", value_key="F", ratio_key="Rt", load_key="L"
        )
        half = next((x for x in scaled if x.load == "half_load"), None)
        check(
            "DatalogReasoner",
            f"半载 = {FULL_LOAD_W} x 0.5 = {HALF_LOAD_W}W",
            half is not None and abs(half.value - HALF_LOAD_W) < 1e-9,
            half.as_fact() if half else "未推出 half_load",
        )
        alias = next((x for x in scaled if x.load == "50pct_load"), None)
        check(
            "DatalogReasoner",
            "别名 50pct_load 缩放结果与半载一致",
            alias is not None and abs(alias.value - HALF_LOAD_W) < 1e-9,
            alias.as_fact() if alias else "未推出 50pct_load",
        )

        # 默认键与种子规则头参数名不符 -> 用默认必 KeyError。
        try:
            scale_bindings(binds, quantity="output_power")
            print("NOTE  scale_bindings 默认键可用(与旧观察不同)")
        except KeyError as e:
            print(
                f"NOTE  scale_bindings 默认键(value/ratio/load)与种子规则头参数名"
                f"(Quantity/Load/FullLoadValue/Ratio)不符 -> KeyError {e};"
                f" 须按查询变量名显式传键"
            )

        try:
            scale(FULL_LOAD_W, "非数值", quantity="output_power", load="half_load")
            check("DatalogReasoner", "反向: 非数值比例抛错", False, "静默返回了值")
        except ValueError:
            check("DatalogReasoner", "反向: 非数值比例抛错(不静默算成 0)", True, "ValueError")

        try:
            r.add_rule("power(X, P) :- voltage(X, V), P = V * 11.1")
            derived = r.query("power(X, P)")
            check(
                "DatalogReasoner",
                "反向: 算术等式被接受但不推导(静默无结论)",
                derived == [],
                f"add_rule 接受, query -> {derived};"
                f" load_scaling.py:18 写的是「会报错」, 实测是**静默接受**",
            )
        except Exception:  # noqa: BLE001
            check("DatalogReasoner", "反向: 算术等式被拒", True, "注释与实测一致")

        src_files = {
            str(p.relative_to(ROOT)): p.read_text(encoding="utf-8", errors="replace")
            for p in (ROOT / "src").rglob("*.py")
        }
        real = [
            f
            for f, t in src_files.items()
            if re.search(r"^\s*(?:from|import)\s+\S*DatalogReasoner", t, re.M)
            or re.search(r"=\s*DatalogReasoner\s*\(", t, re.M)
        ]
        mention = sorted(f for f, t in src_files.items() if "DatalogReasoner" in t)
        check(
            "DatalogReasoner",
            "生产 src/ 零实例化(能力未被服务消费)",
            not real,
            f"仅注释/文档提及 {mention}; 真实 import/实例化 {real or '无'}",
        )
    except Exception as e:  # noqa: BLE001
        check("DatalogReasoner", "推理", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # ------------------------------------------------------------ 9
    section("9  Explorer HTTP 面")
    try:
        base = f"http://127.0.0.1:{s.explorer_port}"
        key = s.explorer_api_key or ""

        def get(path: str, with_key: bool) -> tuple[int, str]:
            req = urllib.request.Request(base + path)
            if with_key and key:
                req.add_header("X-API-Key", key)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.status, resp.read(1200).decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read(200).decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001
                return 0, f"{type(e).__name__}: {e}"

        check("Explorer", "/aterag/live 免鉴权存活探针", get("/aterag/live", False)[0] == 200, "")
        check(
            "Explorer", "/aterag/health 无 key 必须 401", get("/aterag/health", False)[0] == 401, ""
        )
        st_h, body_h = get("/aterag/health", True)
        check("Explorer", "/aterag/health 带 key 200", st_h == 200, f"HTTP {st_h}")
        check(
            "Explorer",
            "health 报鉴权已配置",
            '"auth_configured":true' in body_h.replace(" ", ""),
            body_h[:110],
        )

        loaders = (json.loads(body_h).get("loaders") or {}) if body_h.startswith("{") else {}
        check(
            "Explorer",
            "seed 与 postgres 加载器均 ok",
            (loaders.get("seed") or {}).get("ok") and (loaders.get("postgres") or {}).get("ok"),
            f"seed={loaders.get('seed')} postgres={loaders.get('postgres')}",
        )

        st_g, body_g = get("/aterag/graph/summary", True)
        d = json.loads(body_g) if body_g.strip().startswith("{") else {}
        check(
            "Explorer",
            "graph/summary 返回真实规模",
            st_g == 200 and d.get("entities", 0) > 0,
            f"entities={d.get('entities')} raw={d.get('relations_raw')} "
            f"built={d.get('relations_built')}",
        )
        check(
            "Explorer",
            "raw >= built(自环/外部引用被丢弃)",
            d.get("relations_raw", 0) >= d.get("relations_built", 0),
            f"{d.get('relations_raw')} >= {d.get('relations_built')}",
        )
        et = d.get("entity_types") or {}
        check(
            "Explorer",
            "含需求与领域概念",
            et.get("model/Requirement", 0) > 0 and et.get("power_concept", 0) > 0,
            f"Requirement={et.get('model/Requirement')} power_concept={et.get('power_concept')}",
        )
        check(
            "Explorer", "上游 /api/* 无 key 必须 401", get("/api/graph/stats", False)[0] == 401, ""
        )
        check(
            "Explorer",
            "上游 /api/graph/stats 带 key 200",
            get("/api/graph/stats", True)[0] == 200,
            "",
        )
        check("Explorer", "前端 SPA 可达", get("/", False)[0] == 200, "")
    except Exception as e:  # noqa: BLE001
        check("Explorer", "HTTP 面", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()

    # ------------------------------------------------------------ 汇总
    section("汇总")
    caps: dict[str, list[bool]] = {}
    for cap, _n, ok, _d in RESULTS:
        caps.setdefault(cap, []).append(ok)
    for cap, oks in sorted(caps.items()):
        n_ok, n = sum(oks), len(oks)
        print(f"  {'PASS' if n_ok == n else 'FAIL'}  {cap:<20} {n_ok}/{n}")
    total = sum(1 for *_x, ok, _d in RESULTS if ok)
    print(f"\n  合计 {total}/{len(RESULTS)} 条断言通过")
    fails = [(c, n, d) for c, n, ok, d in RESULTS if not ok]
    if fails:
        print("\n未通过明细:")
        for c, n, d in fails:
            print(f"  FAIL [{c}] {n}\n       {d}")
    else:
        print("\n全部通过")
    return 0 if total == len(RESULTS) else 1


ALT_VALUE = 600.0
SPEC_DOC = "PA601-D54A 定制电源技术规格书.md#4.3.2"
METHOD_DOC = "industry_method/psu_timing_measurement"

if __name__ == "__main__":
    raise SystemExit(main())
