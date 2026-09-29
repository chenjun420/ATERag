"""列出 ag_catalog 里与 graph 相关的函数签名 (清理脚本选型用)."""
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
            "SELECT proname, pg_get_function_identity_arguments(p.oid) "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'ag_catalog' AND proname ILIKE '%graph%' ORDER BY 1"
        )
        for name, args in cur.fetchall():
            print(f"  {name}({args})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
