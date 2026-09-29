"""诊断板卡 LightRAG 各表按 workspace 的落库明细."""

from __future__ import annotations

import sys

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings

TABLES = [
    "lightrag_doc_status",
    "lightrag_doc_full",
    "lightrag_doc_chunks",
    "lightrag_full_entities",
    "lightrag_full_relations",
    "lightrag_entity_chunks",
    "lightrag_relation_chunks",
    "lightrag_vdb_chunks_qwen3_7_text_embedding_1024d",
    "lightrag_vdb_entity_qwen3_7_text_embedding_1024d",
    "lightrag_vdb_relation_qwen3_7_text_embedding_1024d",
    "lightrag_llm_cache",
]


def main() -> int:
    s = get_settings()
    with psycopg.connect(s.postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'lightrag%'"
        )
        present = {r[0] for r in cur.fetchall()}
        for t in TABLES:
            if t not in present:
                print(f"{t}: 不存在")
                continue
            cur.execute("SELECT count(*) FROM public." + t)
            total = cur.fetchone()[0]
            # 逐列找 workspace 类字段
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s AND (column_name LIKE '%%workspace%%' "
                "OR column_name LIKE '%%graph%%' OR column_name=%s)",
                (t, t),
            )
            cols = [r[0] for r in cur.fetchall()]
            dist = ""
            for c in cols:
                cur.execute(
                    f'SELECT "{c}", count(*) FROM public.{t} GROUP BY 1 ORDER BY 2 DESC LIMIT 8'
                )
                dist += f" | {c}={dict(cur.fetchall())}"
            print(f"{t:52s} {total:6d}{dist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
