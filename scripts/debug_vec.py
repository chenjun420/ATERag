"""Qdrant 4.3.3 过滤命中诊断."""

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from aterag.config import get_settings
from aterag.models import EmbeddingClient


async def main():
    embed = EmbeddingClient(get_settings())
    vec = (await embed.embed(["输出过流保护点 电流范围"]))[0]
    client = QdrantClient(url="http://192.168.5.24:6333", timeout=30)
    res = client.query_points(
        collection_name="aterag_chunks",
        query=vec,
        query_filter=Filter(
            must=[
                FieldCondition(key="workspace_id", match=MatchValue(value="PA601-D54A")),
                FieldCondition(key="section_path", match=MatchValue(value="4.3.3")),
            ]
        ),
        limit=10,
        with_payload=True,
    )
    print(f"4.3.3 hits={len(res.points)}")
    for p in res.points:
        pl = p.payload or {}
        has12 = "12" in (pl.get("content") or "")
        has18 = "18" in (pl.get("content") or "")
        print(
            f"  req={pl.get('req_id')} rail={pl.get('rail')} score={p.score:.4f} 12={has12} 18={has18}"
        )
    await embed.aclose()


asyncio.run(main())
