"""LightRAG mix 图检索验收 (PA601 摄取完成后)."""
import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.models import EmbeddingClient
from aterag.rag.service import RagService
from aterag.registry import Registry

KEYWORDS = ("过流", "1309", "12", "18")


async def main() -> int:
    settings = get_settings()
    registry = Registry.load(settings)
    embed = EmbeddingClient(settings)
    if not embed.dimension:
        await embed.probe_dimension()
    rag = RagService(settings, registry, embed)

    r = await rag.search(
        "输出过流保护点是多少", "PA601-D54A", use_graph=True, top_k=4
    )
    print("vector/bm25 results:", len(r["results"]))
    print("graph results:", len(r.get("graph_results", [])))
    hits = r.get("graph_results", [])
    for g in hits[:2]:
        c = g.get("content", "")
        print("---graph hit:", g.get("source"), "len=", len(c))
        for kw in KEYWORDS:
            print(f"  contains {kw!r}: {kw in c}")
    await embed.aclose()

    # ---- 判定: 图检索必须返回"被解析的独立引用块"且含查询关键字 ----
    # 反例: 整段上下文盲截 2000 字符 -> 1 个不含任何关键字的片段 (曾经的缺陷)
    checks: list[tuple[str, bool]] = [
        ("向量/BM25 主检索有结果", len(r["results"]) > 0),
        ("图检索返回 top_k 独立块", len(hits) >= 4),
        ("图检索未落入未解析兜底", all(g.get("source") != "graph-mix-raw" for g in hits)),
        ("图检索块均来自解析后的文档块", all(g.get("source") == "graph-mix" for g in hits)),
        ("图检索块含查询关键字", any(kw in g.get("content", "") for g in hits for kw in KEYWORDS)),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    npass = sum(1 for _, ok in checks if ok)
    print(f"GRAPH_VERIFY {'PASS' if npass == len(checks) else 'FAIL'} {npass}/{len(checks)}")
    return 0 if npass == len(checks) else 1


sys.exit(asyncio.run(main()))
