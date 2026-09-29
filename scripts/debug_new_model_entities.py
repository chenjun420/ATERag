"""检查新型号的实体抽取质量 (对比基准型号).

用法: .venv\\Scripts\\python.exe scripts\\debug_new_model_entities.py PN2000-24A
"""

from __future__ import annotations

import json
import sys

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings

MODEL = sys.argv[1] if len(sys.argv) > 1 else "PN2000-24A"
BASE = "PA601-D54A"


def dump(cur, model: str) -> None:
    cur.execute(
        "SELECT etype, count(*) FROM aterag_entities WHERE model_id=%s GROUP BY etype ORDER BY 1",
        (model,),
    )
    print(f"\n=== {model} 实体类型分布 ===")
    for etype, n in cur.fetchall():
        print(f"  {etype:14s} {n}")
    cur.execute(
        "SELECT eid, props FROM aterag_entities WHERE model_id=%s AND etype='Protection' ORDER BY eid",
        (model,),
    )
    rows = cur.fetchall()
    print(f"  -- Protection ({len(rows)}) --")
    for eid, p in rows[:6]:
        print(f"   {eid}\n     {json.dumps(p, ensure_ascii=False)[:300]}")


def main() -> int:
    s = get_settings()
    with psycopg.connect(s.postgres_dsn) as c, c.cursor() as cur:
        dump(cur, MODEL)
        dump(cur, BASE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
