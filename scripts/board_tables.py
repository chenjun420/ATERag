"""列出板卡 PG 的所有表 (部署排障用)."""

from __future__ import annotations

import sys

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings


def main() -> int:
    s = get_settings()
    with psycopg.connect(s.postgres_dsn) as c, c.cursor() as cur:
        cur.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog','information_schema','ag_catalog') "
            "ORDER BY 1,2"
        )
        for sch, tab in cur.fetchall():
            print(f"{sch}.{tab}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
