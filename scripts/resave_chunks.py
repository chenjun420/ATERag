"""重存型号分块 (幂等): PG 单表写入(chunk + 向量一次完成)。

原先要分别管 PG 行与 Qdrant 点两套 ID 体系; 现在向量与行同表, 一次
``save_chunk_vectors`` 就够 —— 且按内容寻址的 ``chunk_key`` upsert, 重跑幂等。

用途 (方案 §4.6)
----------------
``ingest_spec`` 正常会写向量; 缺向量的批次走的是 ``save_chunks_rows`` (只写行)。
本脚本把缺的那批补齐, 让型号层有第二路召回 —— 检索的向量那一路条件是
``embedding IS NOT NULL``, 所以型号层向量为空时它会被整体排除, 只剩 BM25 单路。

用法:
    .venv\\Scripts\\python.exe scripts/resave_chunks.py -m PA601-D54A -d "F:\\specs\\PA601 规格书.md"

``-d`` **必须给绝对路径或板卡上的真实路径**: 早先这里硬编码了一个相对路径
``"PA601-D54A 定制电源技术规格书.md"``, 它只在开发者那台机器上成立, 板卡上规格书
根本不在那个位置 —— 而脚本的报错只是 ``FileNotFoundError``, 没人猜得到要传参。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import psycopg  # noqa: E402

from aterag.config import get_settings  # noqa: E402
from aterag.ingest.pipeline import (  # noqa: E402
    blocks_to_chunks,
    delete_workspace_chunks,
    ensure_pg_schema,
    parse_markdown,
    save_chunks_rows,
)
from aterag.models import EmbeddingClient  # noqa: E402
from aterag.retrieval import hybrid  # noqa: E402


def resolve_doc(doc: str) -> Path:
    """定位规格书, 并在找不到时给出**可操作**的提示。

    找不到就说「没找到 + 试过哪些路径 + 请用 -d 指定」, 而不是抛
    ``FileNotFoundError`` 让调用方自己去猜要传什么。
    """
    tried = [doc]
    p = Path(doc)
    if p.exists():
        return p
    # 常见摆位顺手试一遍: 只在 -d 没给绝对路径时才试, 且逐个报告
    for cand in (
        Path.cwd() / doc,
        Path.cwd() / "specs" / doc,
        Path("/opt/aterag/specs") / doc,
    ):
        if cand.exists() and cand.resolve() != p.resolve():
            tried.append(str(cand))
            return cand
    raise SystemExit(
        "[FAIL] 找不到规格书。\n"
        f"       试过: {', '.join(tried)}\n"
        "       请用 -d <路径> 显式指定 (板卡上的规格书路径与开发机不同)。\n"
        "       例: -d /opt/aterag/specs/PA601-D54A.md"
    )


async def resave(model: str, doc: str, *, delete_first: bool = True) -> dict:
    """重存一个型号的分块与向量, 返回统计。返回字典而非 print, 便于测试。"""
    settings = get_settings()
    doc_path = resolve_doc(doc)
    embed = EmbeddingClient(settings)
    if not embed.dimension:
        await embed.probe_dimension()

    try:
        text = doc_path.read_text(encoding="utf-8")
        blocks = parse_markdown(text)
        chunks = blocks_to_chunks(blocks, model, layer="model")
        print(f"doc={doc_path}  blocks={len(blocks)}  chunks={len(chunks)}")

        ensure_pg_schema(settings.postgres_dsn)
        hybrid.ensure_vector_schema(settings.postgres_dsn, embed.dimension)

        deleted = delete_workspace_chunks(settings.postgres_dsn, model) if delete_first else 0
        if delete_first:
            print(f"PG deleted={deleted}")

        n = save_chunks_rows(settings.postgres_dsn, chunks)
        print(f"PG saved={n}")

        vecs = await embed.embed([c["content"] for c in chunks])
        n_vec = hybrid.save_chunk_vectors(settings.postgres_dsn, chunks, vecs)
        print(f"PG vectors={n_vec}")

        with psycopg.connect(settings.postgres_dsn) as conn:
            total = conn.execute(
                "SELECT count(*), count(embedding) FROM aterag_chunks WHERE workspace_id = %s",
                (model,),
            ).fetchone()

        # 抽样看内容 —— 早先这里硬编码查 SR-PA601-D54A-1308, 换型号就查不到,
        # 而查不到时脚本静默什么都不打, 让人以为校验通过了。改成按 workspace
        # 抽样, 与型号无关。
        with psycopg.connect(settings.postgres_dsn) as conn:
            sample = conn.execute(
                "SELECT req_id, left(content, 70) FROM aterag_chunks "
                "WHERE workspace_id = %s ORDER BY chunk_id LIMIT 3",
                (model,),
            ).fetchall()
        for rid, content in sample:
            print(f"  sample {rid}: {content}")
        return {
            "model": model,
            "chunks": len(chunks),
            "deleted": deleted,
            "saved": n,
            "vectors": n_vec,
            "rows": total[0],
            "with_embedding": total[1],
        }
    finally:
        await embed.aclose()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="重存型号分块与向量 (幂等)")
    ap.add_argument("-m", "--model", default="PA601-D54A", help="型号 ID")
    ap.add_argument(
        "-d",
        "--doc",
        default="",
        help="规格书 md 路径。**板卡上必须显式给** —— 硬编码的相对路径只在本机成立",
    )
    ap.add_argument(
        "--no-delete",
        action="store_true",
        help="不先删该 workspace 的旧行 (默认删, 因为按 chunk_key upsert 本身幂等)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.doc:
        print("[FAIL] 必须用 -d 指定规格书路径。")
        print("       早先这里硬编码了一个只在本机成立的相对路径, 板卡上跑必然失败 ——")
        print("       与其让 FileNotFoundError 说话, 不如直接要这个参数。")
        print("       例: -d /opt/aterag/specs/PA601-D54A.md")
        return 2
    stats = asyncio.run(resave(args.model, args.doc, delete_first=not args.no_delete))
    rows, vecs = stats["rows"], stats["with_embedding"]
    print(f"\nPG workspace rows={rows} with_embedding={vecs}")
    if rows and vecs < rows:
        print(f"{'❌'} 向量仍不完整: {rows - vecs}/{rows} 条缺 embedding")
        return 1
    print(f"{'✅'} 向量回填完整 ({vecs}/{rows})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
