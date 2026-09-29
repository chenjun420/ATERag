"""诊断板卡 LightRAG doc_status / doc_full 明细 (判断抽取是否跑完)."""
from __future__ import annotations

import sys

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings


def main() -> int:
    s = get_settings()
    with psycopg.connect(s.postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='lightrag_doc_status' ORDER BY ordinal_position")
        print("doc_status 列:", [r[0] for r in cur.fetchall()])
        cur.execute("SELECT id, workspace, status, chunks_count, error_msg "
                    "FROM public.lightrag_doc_status ORDER BY workspace, id")
        for r in cur.fetchall():
            print(f"  {str(r[0])[:12]:12s} ws={r[1]:14s} status={r[2]:8s} chunks={r[3]} err={(r[4] or '')[:60]}")

        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='lightrag_full_entities' ORDER BY ordinal_position")
        print("\nfull_entities 列:", [r[0] for r in cur.fetchall()])
        cur.execute("SELECT id, workspace, count, octet_length(entity_names::text) "
                    "FROM public.lightrag_full_entities ORDER BY workspace")
        print("\nfull_entities (每文档一行, count=合并后实体数):")
        for r in cur.fetchall():
            print(f"  {str(r[0])[:12]:12s} ws={r[1]:14s} count={r[2]} json_len={r[3]}")

        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='lightrag_full_relations' "
                    "ORDER BY ordinal_position")
        rel_cols = [r[0] for r in cur.fetchall()]
        print("\nfull_relations 列:", rel_cols)
        cnt_col = next((c for c in ("count", "relation_count") if c in rel_cols), None)
        if cnt_col:
            cur.execute(f"SELECT id, workspace, {cnt_col} FROM public.lightrag_full_relations "
                        "ORDER BY workspace")
            print("full_relations (每文档一行):")
            for r in cur.fetchall():
                print(f"  {str(r[0])[:12]:12s} ws={r[1]:14s} count={r[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
