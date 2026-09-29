"""Semantica 双写: 把 domain_rules 的结构化规则写入 Semantica 语义图 (PG/AGE).

设计要点:
- 规则本身已是结构化数据 (statement/derive/constraint/source/confidence/test), 无需再过 LLM 抽取,
  因此直接构造 Semantica KnowledgeGraph 的 entities/relationships, 保证可重复、可审计。
- 节点: 每条规则一个 Rule 节点; 另建 Category / Scope / Source 维度节点。
- 关系: Rule -[BELONGS_TO]-> Category, Rule -[IN_SCOPE]-> Scope, Rule -[CITES]-> Source,
  derive 规则 -[HAS_FORMULA]-> Formula 节点, constraint 规则 -[HAS_CONSTRAINT]-> Shape 节点。
- 幂等: 先按 rule_id 清理同名节点再写入, 重复执行不产生重复。

用法: $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts\\sync_semantica.py [domain ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings


def load_rules(rules_dir: Path, domain: str) -> list[dict]:
    rules: list[dict] = []
    for sub in ("common", domain):
        d = rules_dir / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("*.yaml")):
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for r in data.get("rules", []):
                r["_domain"] = sub
                src = r.get("source")
                if isinstance(src, dict) and hasattr(src.get("retrieved"), "isoformat"):
                    src["retrieved"] = src["retrieved"].isoformat()
                rules.append(r)
    return rules


def build_knowledge_graph(domain: str, rules: list[dict]):
    """构造 Semantica KnowledgeGraph (纯结构化, 无 LLM)。"""
    from semantica.kg import KnowledgeGraph

    entities: list[dict] = []
    relationships: list[dict] = []

    def node(key: str, name: str, etype: str, props: dict) -> dict:
        ent = {"id": key, "name": name, "type": etype, "properties": props}
        entities.append(ent)
        return ent

    for r in rules:
        rid = r.get("id")
        if not rid:
            continue
        derive = r.get("derive") or {}
        constraint = r.get("constraint") or {}
        props = {
            "rule_id": rid,
            "domain": r.get("_domain", "common"),
            "category": r.get("category", ""),
            "scope": r.get("scope", ""),
            "statement": r.get("statement", ""),
            "confidence": r.get("confidence", 1.0),
            "rule_kind": "derive" if derive else ("constraint" if constraint else "narrative"),
            "derive_expr": derive.get("expr", ""),
            "derive_output": derive.get("output", ""),
            "derive_inputs": derive.get("inputs", []),
            "has_shape": bool(constraint.get("shape")),
            "test_given": (r.get("test") or {}).get("given", {}),
            "test_expect": (r.get("test") or {}).get("expect", {}),
            "source_name": (r.get("source") or {}).get("name", ""),
            "source_url": (r.get("source") or {}).get("url", ""),
            "source_retrieved": (r.get("source") or {}).get("retrieved", ""),
        }
        node(rid, r.get("statement", rid)[:120], "Rule", props)

        cat = r.get("category", "")
        if cat:
            node(f"cat:{cat}", cat, "Category", {"category": cat})
            relationships.append({"source": rid, "target": f"cat:{cat}", "type": "BELONGS_TO", "properties": {}})

        scope = r.get("scope", "")
        if scope:
            node(f"scope:{scope}", scope, "Scope", {"scope": scope})
            relationships.append({"source": rid, "target": f"scope:{scope}", "type": "IN_SCOPE", "properties": {}})

        url = (r.get("source") or {}).get("url", "")
        name = (r.get("source") or {}).get("name", "")
        if url:
            skey = f"src:{url}"
            node(skey, name or url, "Source", {"name": name, "url": url})
            relationships.append({"source": rid, "target": skey, "type": "CITES", "properties": {}})

        if derive.get("expr"):
            fkey = f"formula:{rid}"
            node(fkey, derive.get("output", "result"), "Formula", {
                "expr": derive["expr"],
                "output": derive.get("output", ""),
                "inputs": derive.get("inputs", []),
            })
            relationships.append({"source": rid, "target": fkey, "type": "HAS_FORMULA", "properties": {}})

        if constraint.get("shape"):
            ckey = f"shape:{rid}"
            node(ckey, f"{rid} SHACL shape", "Shape", {"shape": constraint["shape"]})
            relationships.append({"source": rid, "target": ckey, "type": "HAS_CONSTRAINT", "properties": {}})

    kg = KnowledgeGraph(
        entities=entities,
        relationships=relationships,
        metadata={"domain": domain, "source": "domain_rules"},
    )
    return kg


class _AgeStoreNoLoad:
    """ApacheAgeStore 的最小侵入子类: 跳过需要超级用户权限的 LOAD 'age'。

    板卡 PG 上 age 扩展已随 LightRAG 部署就绪, 且 powerspec 用户可访问
    ag_catalog (LightRAG 通过 SET search_path 使用同一能力), 但 LOAD 'age'
    需要超级用户, 因此这里只做 search_path + 建图, 复用父类全部 Cypher 逻辑。
    """

    @staticmethod
    def build(connection_string: str, graph_name: str):
        from semantica.graph_store import ApacheAgeStore

        class AgeStore(ApacheAgeStore, _AgeStoreNoLoad):  # type: ignore[misc,valid-type]
            def connect(self, **options) -> bool:
                import psycopg2

                conn_str = options.get("connection_string", self.connection_string)
                self._conn = psycopg2.connect(conn_str)
                self._conn.autocommit = False
                with self._conn.cursor() as cur:
                    cur.execute('SET search_path = ag_catalog, "$user", public;')
                    cur.execute(
                        "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s;",
                        (self.graph_name,),
                    )
                    (count,) = cur.fetchone()
                    if count == 0:
                        cur.execute("SELECT create_graph(%s);", (self.graph_name,))
                self._conn.commit()
                return True

        return AgeStore(connection_string=connection_string, graph_name=graph_name)


def main(domains: list[str]) -> int:
    s = get_settings()
    rules_dir = Path(s.domain_rules_dir)
    if not s.semantica_enabled:
        print("Semantica 未启用 (SEMANTICA_ENABLED=false), 跳过")
        return 0

    store = _AgeStoreNoLoad.build(s.postgres_dsn, s.semantica_graph)
    if not store.connect():
        print("Semantica AGE 连接失败, fail-fast")
        return 2

    for domain in domains:
        rules = load_rules(rules_dir, domain)
        kg = build_knowledge_graph(domain, rules)
        ids: dict[str, int] = {}
        for e in kg.entities:
            key = e["id"]
            # 幂等: 同一 key 的旧节点先删除 (含其边), 再重建, 重复执行不产生重复
            store.execute_query(
                f"MATCH (n:{e['type']}) WHERE n.key = $key DETACH DELETE n",
                parameters={"key": key},
            )
            res = store.create_node(labels=[e["type"]], properties={"key": key, **e["properties"]})
            ids[key] = int(res["id"]) if isinstance(res, dict) and "id" in res else -1

        for rel in kg.relationships:
            s_id, t_id = ids.get(rel["source"]), ids.get(rel["target"])
            if s_id is None or t_id is None or s_id < 0 or t_id < 0:
                continue
            store.create_relationship(s_id, t_id, rel["type"], rel.get("properties") or {})

        print(f"SEMANTICA_SYNC domain={domain} entities={len(kg.entities)} relationships={len(kg.relationships)}")
        stats = store.get_stats()
        print("  graph stats:", json.dumps(stats, ensure_ascii=False, default=str))

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["power"]))
