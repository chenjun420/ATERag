"""清理板卡上的脏 LightRAG workspace (大写/未归一化) 与重复插入记录.

背景: LightRAG merge 阶段以不带引号的方式拼接 AGE 标识符, PostgreSQL 折叠大写,
导致图谱写入失败但 KV/向量层已落库 —— 表现为 doc_status=failed 却残留分块与实体向量。

用法: $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts\\board_clean_lrag.py --dry-run
      .venv\\Scripts\\python.exe scripts\\board_clean_lrag.py --apply
"""
from __future__ import annotations

import argparse
import sys

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.ingest.pipeline import lrag_workspace

# 脏 workspace: 未归一化 (lrag_workspace 会改变它)
DIRTY = ["PA601-D54A"]

LRAG_TABLES = [
    "lightrag_doc_status",
    "lightrag_doc_chunks",
    "lightrag_doc_full",
    "lightrag_full_entities",
    "lightrag_full_relations",
    "lightrag_entity_chunks",
    "lightrag_relation_chunks",
    "lightrag_llm_cache",
]

VDB_TABLES = [
    t for t in (
        "lightrag_vdb_chunks_qwen3_7_text_embedding_1024d",
        "lightrag_vdb_entity_qwen3_7_text_embedding_1024d",
        "lightrag_vdb_relation_qwen3_7_text_embedding_1024d",
    )
]


def counts(cur, table: str, workspace: str) -> int:
    cur.execute(f"SELECT count(*) FROM public.{table} WHERE workspace = %s", (workspace,))
    return cur.fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行删除 (缺省仅预览)")
    ap.add_argument("--drop-duplicates", action="store_true",
                    help="同时删除 doc_status 中 status=failed 的 dup-* 重复插入记录")
    args = ap.parse_args()

    s = get_settings()
    for w in DIRTY:
        assert w != lrag_workspace(w), f"{w} 实为已归一化, 不应清理"

    with psycopg.connect(s.postgres_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename LIKE 'lightrag%' ORDER BY 1")
        present = {r[0] for r in cur.fetchall()}
        tables = [t for t in LRAG_TABLES + VDB_TABLES if t in present]

        plan: list[str] = []
        for w in DIRTY:
            print(f"=== 脏 workspace {w} ===")
            for t in tables:
                n = counts(cur, t, w)
                if n:
                    print(f"  {t:52s} {n:6d}")
                    plan.append(f"DELETE FROM public.{t} WHERE workspace = '{w}';  -- {n} 行")
            # AGE graph: LightRAG 命名 {workspace}_{namespace}, '-' 转 '_'。
            # 注意不能 lower(): 脏 workspace 以带引号方式建图, AGE 里保留了原始大写,
            # 归一化后的名字会撞上正确的同名图。
            graph = f"{w.replace('-', '_')}_chunk_entity_relation"
            cur.execute("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (graph,))
            if cur.fetchone()[0]:
                print(f"  AGE graph {graph:43s} 存在")
                plan.append(f"DROP GRAPH {graph} CASCADE;")
            else:
                print(f"  AGE graph {graph:43s} 不存在")

        if args.drop_duplicates:
            cur.execute("SELECT workspace, count(*) FROM public.lightrag_doc_status "
                        "WHERE status <> 'processed' GROUP BY workspace ORDER BY workspace")
            dups = cur.fetchall()
            for ws, n in dups:
                print(f"=== 重复插入记录 workspace={ws}: {n} 条 ===")
                plan.append(f"DELETE FROM public.lightrag_doc_status WHERE workspace = '{ws}' "
                            f"AND status <> 'processed';  -- {n} 行")

        if not plan:
            print("\n无需清理")
            return 0
        print("\n=== 清理计划 ===")
        for p in plan:
            print("  " + p)
        if not args.apply:
            print("\n(预览模式, 加 --apply 执行)")
            return 0

        print("\n=== 执行 ===")
        for w in DIRTY:
            for t in tables:
                cur.execute(f"DELETE FROM public.{t} WHERE workspace = %s", (w,))
                if cur.rowcount:
                    print(f"  删除 {t} workspace={w}: {cur.rowcount} 行")
            graph = f"{w.replace('-', '_')}_chunk_entity_relation"
            cur.execute("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (graph,))
            if cur.fetchone()[0]:
                # DROP GRAPH 是 AGE 的 Cypher 命令, 不是 SQL; 用 ag_catalog.drop_graph(name, cascade)
                # 这一 SQL 入口 (可直接传参 —— AGE 的 cypher() 不支持参数绑定, 只能字面量内联)
                cur.execute("SELECT ag_catalog.drop_graph(%s, true)", (graph,))
                print(f"  DROP GRAPH {graph} CASCADE -> {cur.fetchone()[0]}")
        if args.drop_duplicates:
            cur.execute("DELETE FROM public.lightrag_doc_status WHERE status <> 'processed'")
            print(f"  删除失败/重复插入记录: {cur.rowcount} 行")

    print("CLEAN_LRAG DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
