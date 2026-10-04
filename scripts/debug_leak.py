"""诊断 30A 泄漏: 4.3.3 过滤下哪个组块含 '30'."""

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
        "输出过流保护点",
        8,
        section_path="4.3.3",
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
