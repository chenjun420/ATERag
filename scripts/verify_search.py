"""检索链早期验证 (BM25 + Qdrant + RRF, 不依赖 LightRAG)."""

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from qdrant_client import QdrantClient

from aterag.config import get_settings
from aterag.ingest.pipeline import bm25_search, qdrant_search, rrf_fuse
from aterag.models import EmbeddingClient


async def main():
    settings = get_settings()
    embed = EmbeddingClient(settings)
    if not embed.dimension:
        await embed.probe_dimension()
    qdrant = QdrantClient(url=settings.qdrant_url, timeout=60)

    # 1) BM25 中文检索: 过流保护 (仅 PA601 workspace)
    bm = bm25_search(settings.postgres_dsn, ["PA601-D54A"], "输出过流保护点", 8)
    print(f"BM25 hits={len(bm)}")
    for h in bm[:3]:
        print("  ", h["section_path"], h["req_id"], h["content"][:60].replace("\n", " "))

    # 2) 章节过滤向量检索: 4.3.3 应命中 12~18
    vec = await qdrant_search(
        qdrant, embed, ["PA601-D54A"], "输出过流保护点", 6, section_path="4.3.3"
    )
    joined = " ".join(h.get("content", "") for h in vec)
    print(f"VEC(4.3.3) hits={len(vec)} has12={('12' in joined)} has18={('18' in joined)}")
    has_111 = any("11.1" in h.get("content", "") for h in vec)
    print(f"  11.1 泄漏={has_111}")

    # 3) RRF 融合
    fused = rrf_fuse(vec, bm, top_k=5)
    print(f"RRF fused={len(fused)} top_score={fused[0]['rrf_score'] if fused else '-'}")
    for h in fused[:2]:
        print("  ", h.get("section_path"), h.get("content", "")[:50].replace("\n", " "))

    await embed.aclose()


asyncio.run(main())
