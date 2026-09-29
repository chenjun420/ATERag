"""诊断 30A 泄漏: 4.3.3 过滤下哪个组块含 '30'."""

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from qdrant_client import QdrantClient

from aterag.config import get_settings
from aterag.ingest.pipeline import qdrant_search
from aterag.models import EmbeddingClient


async def main():
    embed = EmbeddingClient(get_settings())
    qdrant = QdrantClient(url="http://192.168.5.24:6333", timeout=60)
    hits = await qdrant_search(
        qdrant, embed, ["PA601-D54A"], "输出过流保护点", 8, section_path="4.3.3"
    )
    for h in hits:
        c = h.get("content", "")
        if "30" in c.replace("2026", "").replace("300", ""):
            print("LEAK section=", h.get("section_path"), "req=", h.get("req_id"))
            for line in c.splitlines():
                if "30" in line:
                    print("  LINE:", line[:110])
    await embed.aclose()


asyncio.run(main())
