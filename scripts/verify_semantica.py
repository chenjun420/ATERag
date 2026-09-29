"""Semantica 语义图回读校验: 验证规则双写可查询、字段完整。

用法: $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts\\verify_semantica.py [domain]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")
sys.stdout.reconfigure(encoding="utf-8")

from sync_semantica import _AgeStoreNoLoad, load_rules

from aterag.config import get_settings


def main(domain: str) -> int:
    s = get_settings()
    rules = load_rules(Path(s.domain_rules_dir), domain)
    store = _AgeStoreNoLoad.build(s.postgres_dsn, s.semantica_graph)
    store.connect()

    stats = store.get_stats()
    rule_nodes = stats["label_counts"].get("Rule", 0)
    ok = rule_nodes == len(rules)
    print(f"[1] Rule 节点数 {rule_nodes} == rules.yaml {len(rules)} -> {'PASS' if ok else 'FAIL'}")

    sample = rules[0]["id"]
    rec = store.execute_query(
        "MATCH (r:Rule) WHERE r.rule_id = $rid "
        "RETURN r.rule_id AS rid, r.category AS cat, r.confidence AS conf, r.derive_expr AS expr",
        parameters={"rid": sample},
    )
    r_ok = len(rec["records"]) == 1
    print(f"[2] 按 rule_id 回读 {sample} -> {'PASS' if r_ok else 'FAIL'}")
    if r_ok:
        print("   ", json.dumps(rec["records"][0], ensure_ascii=False, default=str)[:300])

    cnt = store.execute_query("MATCH (r:Rule)-[:HAS_FORMULA]->(f:Formula) RETURN count(r) AS n")
    f_ok = cnt["records"][0]["n"] > 0
    print(
        f"[3] 公式关系可查 (HAS_FORMULA {cnt['records'][0]['n']} 条) -> {'PASS' if f_ok else 'FAIL'}"
    )

    cnt = store.execute_query("MATCH (r:Rule)-[:HAS_CONSTRAINT]->(x:Shape) RETURN count(r) AS n")
    c_ok = cnt["records"][0]["n"] > 0
    print(
        f"[4] 约束关系可查 (HAS_CONSTRAINT {cnt['records'][0]['n']} 条) -> {'PASS' if c_ok else 'FAIL'}"
    )

    cnt = store.execute_query("MATCH (r:Rule)-[:CITES]->(x:Source) RETURN count(r) AS n")
    src_ok = cnt["records"][0]["n"] == len(rules)
    print(f"[5] 来源溯源完整 (CITES {cnt['records'][0]['n']} 条) -> {'PASS' if src_ok else 'FAIL'}")

    print("  stats:", json.dumps(stats, ensure_ascii=False, default=str))
    store.close()
    all_ok = all([ok, r_ok, f_ok, c_ok, src_ok])
    print("SEMANTICA_VERIFY", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "power"))
