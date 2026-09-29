"""列出板卡 PG 业务表结构 (部署排障用)."""
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
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name IN "
            "('aterag_chunks','aterag_entities') ORDER BY table_name, ordinal_position"
        )
        for tab, col, typ in cur.fetchall():
            print(f"{tab:20s} {col:28s} {typ}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
