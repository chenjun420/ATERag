"""诊断 LightRAG mix 图检索返回结构与截断 (部署排障用)."""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.models import EmbeddingClient
from aterag.rag.service import RagService
from aterag.registry import Registry


async def main() -> int:
    s = get_settings()
    registry = Registry.load(s)
    embed = EmbeddingClient(s)
    if not embed.dimension:
        await embed.probe_dimension()
    rag = RagService(s, registry, embed)
    from lightrag import QueryParam

    lrag = rag._get_lightrag("PA601-D54A")
    await lrag.initialize_storages()
    res = await lrag.aquery("输出过流保护点是多少", param=QueryParam(mode="mix", top_k=4, only_need_context=True))
    print("type:", type(res).__name__)
    if isinstance(res, dict):
        print("keys:", list(res.keys()))
        ch = res.get("chunks", {})
        print("chunks type:", type(ch).__name__, "keys:", list(ch.keys()) if isinstance(ch, dict) else "-")
        if isinstance(ch, dict):
            items = ch.get("chunks", [])
            print("n chunks:", len(items))
            for c in items[:4]:
                print("  -", str(c)[:160])
    else:
        text = str(res)
        lines = text.splitlines()
        print("--- Document Chunks 段落 ---")
        for ln in lines[26:49]:
            print("   ", ln[:220])
    await embed.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
