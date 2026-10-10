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
    for sub in (domain,):
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
    """构造 Semantica KnowledgeGraph (纯结构化, 无 LLM)。

    **记录构造复用** :func:`aterag.kg.rule_graph.build_rule_records` —— 分析图
    (``collect_records``)与 AGE 同步跑的是同一批规则, 两边各写一份必然漂:
    同一批规则在 AGE 与在分析图里长成两个样子时, 没人说得清哪个是真的。
    这里只做「记录 -> KnowledgeGraph 容器」的转换。
    """
    from semantica.kg import KnowledgeGraph

    from aterag.kg.rule_graph import build_rule_records

    rec_ents, rec_rels = build_rule_records(rules, domain=domain)

    entities = [
        {
            "id": e["id"],
            "name": e.get("name") or e["id"],
            "type": e["entity_type"],
            "properties": dict(e.get("metadata") or {}),
        }
        for e in rec_ents
    ]
    relationships = [
        {
            "source": r["source_id"],
            "target": r["target_id"],
            "type": r["relationship_type"],
            "properties": {"clause": r.get("clause")} if r.get("clause") else {},
        }
        for r in rec_rels
    ]

    kg = KnowledgeGraph(
        entities=entities,
        relationships=relationships,
        metadata={"domain": domain, "source": "domain_rules"},
    )
    return kg


class _AgeStoreNoLoad:
    """ApacheAgeStore 的最小侵入子类: 跳过需要超级用户权限的 LOAD 'age'。

    板卡 PG 上 age 扩展已就绪, 且 powerspec 用户可访问 ag_catalog, 但 LOAD 'age'
    需要超级用户, 因此这里只做 search_path + 建图, 复用父类全部 Cypher 逻辑。

    **为什么不给服务角色提权**
    ------------------------
    让 ``ApacheAgeStore.connect()`` 原样可用有两条路: 把 ``powerspec`` 提成
    超级用户, 或走这条 search_path 旁路。前者是**安全降级** —— 应用角色一旦是
    超级用户, 任何注入/误操作都能读写整库, 而它本该只碰自己的三张表。所以选
    旁路: 权限模型不变, 代价只是多一个子类。

    **代价要写清楚**: 旁路依赖 ``search_path = ag_catalog``, 而 ``connect()``
    不再执行上游的 ``LOAD 'age'``。上游若在升级中改用 ``LOAD`` 之外的初始化
    (比如临时表、扩展 GUC), 这条旁路会静默失效 —— 所以
    ``tests/test_age_store_access.py`` 钉住「官方 API 仍失败 + 旁路仍可用」
    这个组合, 任一边变了都会红。
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


def age_access_paths(dsn: str, graph_name: str) -> dict:
    """两条访问路径各试一次, 返回各自结果 —— 用于门禁与运维自查。

    同时返回两条而不是只返回能用的那条: 「官方 API 可用了」是**权限模型变更**
    的信号(有人提了权), 那时 ``_AgeStoreNoLoad`` 就该重新评估是否还有必要。
    只报成功的那条会把这个信号吞掉。
    """
    from semantica.graph_store import ApacheAgeStore

    out: dict = {"official": None, "bypass": None}
    official = ApacheAgeStore(connection_string=dsn, graph_name=graph_name)
    try:
        out["official"] = "ok" if official.connect() else "connect() returned False"
    except Exception as e:  # noqa: BLE001
        out["official"] = f"{type(e).__name__}: {str(e)[:90]}"
    shim = _AgeStoreNoLoad.build(dsn, graph_name)
    try:
        out["bypass"] = "ok" if shim.connect() else "connect() returned False"
        out["stats"] = shim.get_stats()
    except Exception as e:  # noqa: BLE001
        out["bypass"] = f"{type(e).__name__}: {str(e)[:90]}"
    finally:
        try:
            shim.close()
        except Exception:  # noqa: BLE001, S110
            pass
    return out


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

        print(
            f"SEMANTICA_SYNC domain={domain} entities={len(kg.entities)} relationships={len(kg.relationships)}"
        )
        stats = store.get_stats()
        print("  graph stats:", json.dumps(stats, ensure_ascii=False, default=str))

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["power"]))
