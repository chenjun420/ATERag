"""PN1000-48A 迷你规格书摄取 (隔离测试第二型号)."""
import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.ingest.pipeline import ingest_spec
from aterag.models import EmbeddingClient, LLMClient
from aterag.registry import Registry

DOC = "tests/fixtures/PN1000-48A 迷你规格书.md"


async def main() -> int:
    settings = get_settings()
    registry = Registry.load(settings)
    llm = LLMClient(settings)
    embed = EmbeddingClient(settings)
    if not embed.dimension:
        await embed.probe_dimension()
    result = await ingest_spec(DOC, settings, registry, embed, llm)
    print("INGEST_RESULT:", result)
    await llm.aclose()
    await embed.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
