"""重存型号分块 (幂等): PG 单表写入(chunk + 向量一次完成)。

原先要分别管 PG 行与 Qdrant 点两套 ID 体系; 现在向量与行同表, 一次
``save_chunk_vectors`` 就够 —— 且按内容寻址的 ``chunk_key`` upsert, 重跑幂等。
"""

import asyncio
import sys

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.ingest.pipeline import (
    blocks_to_chunks,
    delete_workspace_chunks,
    ensure_pg_schema,
    parse_markdown,
    save_chunks_rows,
)
from aterag.models import EmbeddingClient
from aterag.retrieval import hybrid

MODEL = sys.argv[1] if len(sys.argv) > 1 else "PA601-D54A"
DOC = sys.argv[2] if len(sys.argv) > 2 else "PA601-D54A 定制电源技术规格书.md"


async def main() -> int:
    settings = get_settings()
    embed = EmbeddingClient(settings)
    if not embed.dimension:
        await embed.probe_dimension()

    text = open(DOC, encoding="utf-8").read()
    blocks = parse_markdown(text)
    chunks = blocks_to_chunks(blocks, MODEL, layer="model")
    print(f"chunks={len(chunks)}")

    ensure_pg_schema(settings.postgres_dsn)
    hybrid.ensure_vector_schema(settings.postgres_dsn, embed.dimension)
    deleted = delete_workspace_chunks(settings.postgres_dsn, MODEL)
    print(f"PG deleted={deleted}")

    n = save_chunks_rows(settings.postgres_dsn, chunks)
    print(f"PG saved={n}")

    vecs = await embed.embed([c["content"] for c in chunks])
    n_vec = hybrid.save_chunk_vectors(settings.postgres_dsn, chunks, vecs)
    print(f"PG vectors={n_vec}")

    with psycopg.connect(settings.postgres_dsn) as conn:
        total = conn.execute(
            "SELECT count(*), count(embedding) FROM aterag_chunks WHERE workspace_id = %s",
            (MODEL,),
        ).fetchone()
    print(f"PG workspace rows={total[0]} with_embedding={total[1]}")

    # 验证 1308 组块
    with psycopg.connect(settings.postgres_dsn) as conn:
        rows = conn.execute(
            "SELECT content FROM aterag_chunks "
            "WHERE workspace_id = %s AND req_id = %s LIMIT 3",
            (MODEL, "SR-PA601-D54A-1308"),
        ).fetchall()
    for (c,) in rows:
        print(f"1308-chunk has12={'12' in c} has18={'18' in c} len={len(c)}")
    await embed.aclose()
    return 0


asyncio.run(main())
