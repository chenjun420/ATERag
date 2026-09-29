"""板卡 (192.168.5.24) 部署前置检查: 存储栈健康 + 知识落库现状.

只读, 不写任何数据。退出码 0 = 存储栈就绪。

用法: $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts\\board_preflight.py
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings

REQUIRED_EXT = {"vector", "age", "pg_textsearch", "zhparser"}


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:34s} {detail}")
    return ok


def main() -> int:
    s = get_settings()
    host = s.postgres_dsn.split("@")[1].split(":")[0]
    print(f"=== 板卡预检 {host} ===")
    results: list[bool] = []

    # ---- Qdrant ----
    t0 = time.time()
    try:
        with urllib.request.urlopen(f"{s.qdrant_url}/healthz", timeout=10) as r:
            body = r.read().decode().strip()
        results.append(check("Qdrant /healthz", r.status == 200, f"{body} ({time.time()-t0:.2f}s)"))
    except (urllib.error.URLError, OSError) as e:
        results.append(check("Qdrant /healthz", False, str(e)[:80]))

    # ---- PG 连接 (带重试, 板卡偶发 10013) ----
    conn = None
    for attempt in range(1, 6):
        try:
            conn = psycopg.connect(s.postgres_dsn, connect_timeout=10)
            break
        except Exception as e:  # noqa: BLE001
            print(f"    PG 连接第 {attempt} 次失败: {type(e).__name__} {str(e)[:60]}")
            time.sleep(3)
    if conn is None:
        results.append(check("PostgreSQL 连接", False, "5 次重试均失败"))
        return 1
    results.append(check("PostgreSQL 连接", True, conn.info.server_version))

    with conn, conn.cursor() as cur:
        cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname")
        exts = dict(cur.fetchall())
        for e in sorted(REQUIRED_EXT):
            results.append(check(f"PG 扩展 {e}", e in exts, exts.get(e, "缺失")))

        cur.execute("SELECT name FROM ag_catalog.ag_graph ORDER BY name")
        graphs = [r[0] for r in cur.fetchall()]
        print(f"    AGE graphs: {graphs}")
        results.append(check("AGE graph 存在", bool(graphs), f"{len(graphs)} 个"))
        results.append(check("Semantica graph power_rules", "power_rules" in graphs))

        cur.execute(
            "SELECT model_id, count(*) FROM aterag_entities GROUP BY model_id ORDER BY model_id"
        )
        ents = cur.fetchall()
        results.append(check("结构化实体已入库", bool(ents), str(dict(ents))))

        cur.execute("SELECT workspace_id, count(*) FROM aterag_chunks GROUP BY workspace_id ORDER BY workspace_id")
        chunks = cur.fetchall()
        results.append(check("分块已入库", bool(chunks), str(dict(chunks))))

        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        tables = [r[0] for r in cur.fetchall()]
        results.append(check("业务表齐备", "aterag_chunks" in tables and "aterag_entities" in tables,
                             f"{len(tables)} 表"))

    ok = all(results)
    print("BOARD_PREFLIGHT", "PASS" if ok else "FAIL", f"({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
