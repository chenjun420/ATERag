"""重存型号分块 (幂等清理): PG 去重重建 + Qdrant 确定性 ID 重写."""

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

from aterag.config import get_settings
from aterag.ingest.pipeline import (
    CHUNK_COLLECTION,
    blocks_to_chunks,
    delete_workspace_chunks,
    ensure_pg_schema,
    ensure_qdrant,
    parse_markdown,
)
from aterag.models import EmbeddingClient

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
    deleted = delete_workspace_chunks(settings.postgres_dsn, MODEL)
    print(f"PG deleted={deleted}")

    from aterag.ingest.pipeline import save_chunks_rows

    n = save_chunks_rows(settings.postgres_dsn, chunks)
    print(f"PG saved={n}")

    qdrant = QdrantClient(url=settings.qdrant_url, timeout=120)
    ensure_qdrant(qdrant, embed.dimension)
    qdrant.delete(
        collection_name=CHUNK_COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="workspace_id", match=MatchValue(value=MODEL))]
        ),
    )
    vecs = await embed.embed([c["content"] for c in chunks])
    import uuid

    ns = uuid.UUID("a7e2c9d4-0000-4000-8000-1a7e00000001")
    points = [
        PointStruct(
            id=str(uuid.uuid5(ns, f"{MODEL}:{i}")),
            vector=v,
            payload=c,
        )
        for i, (c, v) in enumerate(zip(chunks, vecs))
    ]
    for i in range(0, len(points), 256):
        qdrant.upsert(collection_name=CHUNK_COLLECTION, points=points[i : i + 256])
    info = qdrant.count(
        CHUNK_COLLECTION,
        count_filter=Filter(
            must=[FieldCondition(key="workspace_id", match=MatchValue(value=MODEL))]
        ),
        exact=True,
    )
    print(f"QDRANT workspace points={info.count}")

    # 验证 1309 组块
    hits = qdrant.scroll(
        collection_name=CHUNK_COLLECTION,
        scroll_filter=Filter(
            must=[
                FieldCondition(key="workspace_id", match=MatchValue(value=MODEL)),
                FieldCondition(key="req_id", match=MatchValue(value="SR-PA601-D54A-1308")),
            ]
        ),
        limit=3,
        with_payload=True,
    )
    for p in hits[0]:
        c = p.payload.get("content", "")
        print(f"1308-chunk has12={'12' in c} has18={'18' in c} len={len(c)}")
    await embed.aclose()
    return 0


asyncio.run(main())
