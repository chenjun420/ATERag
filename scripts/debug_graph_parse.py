"""诊断 _parse_graph_context 段落解析 (部署排障用)."""
from __future__ import annotations

import asyncio
import json
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
    ctx = str(await lrag.aquery("输出过流保护点是多少",
                                param=QueryParam(mode="mix", top_k=4, only_need_context=True)))
    print("len:", len(ctx))
    for kw in ("Knowledge Graph Data (Entity)", "Knowledge Graph Data (Relationship)", "Document Chunks", "Reference Document List"):
        print(f"  find({kw!r}) = {ctx.find(kw)}")
    i = ctx.find("Document Chunks")
    fence = ctx.find("```json", i)
    print("  fence idx:", fence)
    end = ctx.find("```", fence + 7)
    print("  end idx:", end)
    raw = ctx[fence + 7 : end]
    print("  raw len:", len(raw))
    print("  raw head:", raw[:120].replace("\n", " "))
    try:
        data = json.loads(raw)
        print("  parsed OK:", type(data).__name__, len(data) if isinstance(data, list) else "")
    except json.JSONDecodeError as e:
        print("  JSON FAIL:", e)
        print("  raw tail:", raw[-200:].replace("\n", " "))
    await embed.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
