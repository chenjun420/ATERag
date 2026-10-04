"""pgvector 章节过滤命中诊断 (原 Qdrant 版, 已迁到 PG 向量列)."""

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.models import EmbeddingClient
from aterag.retrieval import hybrid


async def main():
    settings = get_settings()
    embed = EmbeddingClient(settings)
    hits = await hybrid.vector_search(
        settings.postgres_dsn,
        embed,
        ["PA601-D54A"],
        "输出过流保护点 电流范围",
        10,
        section_path="4.3.3",
    )
    print(f"4.3.3 hits={len(hits)}")
    for h in hits:
        c = h.get("content") or ""
        print(
            f"  req={h.get('req_id')} rail={h.get('rail')} "
            f"score={h.get('score', 0.0):.4f} 12={'12' in c} 18={'18' in c}"
        )
    await embed.aclose()


asyncio.run(main())
