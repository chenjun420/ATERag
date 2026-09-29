"""构建领域知识库 CLI: python scripts/build_domain.py power [common ...]."""

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.ingest.pipeline import build_domain
from aterag.models import EmbeddingClient, LLMClient
from aterag.registry import Registry


async def main() -> int:
    domains = sys.argv[1:] or ["power"]
    settings = get_settings()
    registry = Registry.load(settings)
    llm = LLMClient(settings)
    embed = EmbeddingClient(settings)
    if not embed.dimension:
        await embed.probe_dimension()
    for d in domains:
        result = await build_domain(d, settings, registry, embed, llm)
        print(f"BUILT {d}: {result}")
    await llm.aclose()
    await embed.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
