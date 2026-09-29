"""Semantica 语义图实况核查: 物理存储位置 + 节点/边统计 + 运行时接入判定.

用法: .venv\\Scripts\\python.exe scripts\\board_semantica_status.py
"""
from __future__ import annotations

import sys

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings

LABELS = ["Rule", "Category", "Scope", "Source", "Formula", "Shape"]
EDGES = ["BELONGS_TO", "CITES", "IN_SCOPE", "HAS_FORMULA", "HAS_CONSTRAINT"]


def main() -> int:
    s = get_settings()
    g = s.semantica_graph
    print(f"=== Semantica 语义图实况 (graph={g}) ===")
    print(f"配置: SEMANTICA_ENABLED={s.semantica_enabled} SEMANTICA_GRAPH={g}\n")
    with psycopg.connect(s.postgres_dsn) as c, c.cursor() as cur:
        cur.execute("SELECT name FROM ag_catalog.ag_graph WHERE name = %s", (g,))
        if not cur.fetchone():
            print(f"图谱 {g} 不存在")
            return 1

        # 物理落点: AGE 每个 label / 边类型一张基表 (pg_class 里同名的还有索引, 需 relkind='r' 过滤)
        cur.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'r' "
            "AND c.relname NOT LIKE 'ag_label%%' ORDER BY c.relname", (g,)
        )
        print("--- 物理基表 (schema = graph 名) ---")
        for (rel,) in cur.fetchall():
            cur.execute(f'SELECT count(*) FROM "{g}"."{rel}"')
            print(f"  {rel:16s} rows={cur.fetchone()[0]}")

        print("\n--- 按 label 统计节点 ---")
        total = 0
        for lb in LABELS:
            cur.execute(f'SELECT count(*) FROM "{g}"."{lb}"')
            n = cur.fetchone()[0]
            total += n
            if n:
                print(f"  {lb:12s} {n}")
        print(f"  节点合计 {total}")

        print("\n--- 按类型统计边 ---")
        etotal = 0
        for et in EDGES:
            try:
                cur.execute(f'SELECT count(*) FROM "{g}"."{et}"')
                n = cur.fetchone()[0]
                etotal += n
                if n:
                    print(f"  {et:16s} {n}")
            except psycopg.Error:
                c.rollback()
        print(f"  边合计 {etotal}")

    print(f"\n结论: 数据存于 PostgreSQL {s.postgres_dsn.split('/')[-1]} 的 AGE 图谱 {g} (schema),")
    print("      与 LightRAG 的 {workspace}_chunk_entity_relation 图谱彼此独立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
