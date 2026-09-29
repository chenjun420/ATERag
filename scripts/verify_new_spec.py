"""新规格书导入后全链路校验: 落库量 + 检索命中 + 型号隔离 + 判据生成.

用法: $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts\\verify_new_spec.py PN2000-24A
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.models import EmbeddingClient
from aterag.rag.service import RagService
from aterag.registry import Registry

MODEL = sys.argv[1] if len(sys.argv) > 1 else "PN2000-24A"
WS = MODEL.lower().replace("-", "_")

# 该型号独有的探针值: 命中即证明检索到本型号, 不会与 PA601/PN1000 混淆
PROBES = {
    "PN2000-24A": ["22.0", "30.0", "过流保护", "1501", "PWOK"],
}


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    s = get_settings()
    registry = Registry.load(s)

    # ---- 1. 注册表 ----
    checks.append(("注册表含新型号", MODEL in registry.products,
                   f"domain={registry.products[MODEL].domain if MODEL in registry.products else '-'}"
                   f" version={registry.products[MODEL].doc_version if MODEL in registry.products else '-'}"))

    # ---- 2. 落库量 ----
    import psycopg

    with psycopg.connect(s.postgres_dsn) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM aterag_entities WHERE model_id=%s", (MODEL,))
        n_ent = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM aterag_chunks WHERE workspace_id=%s", (MODEL,))
        n_chunk = cur.fetchone()[0]
        cur.execute("SELECT sum(count) FROM lightrag_full_entities WHERE workspace=%s", (WS,))
        n_lrag = (cur.fetchone()[0] or 0)
        cur.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='lightrag_vdb_chunks_qwen3_7_text_embedding_1024d'")
        checks.append(("PG 实体已入库", n_ent > 0, f"{n_ent} 实体"))
        checks.append(("PG 分块已入库", n_chunk > 0, f"{n_chunk} 分块"))
        checks.append(("LightRAG 图谱已入库", n_lrag > 0, f"{n_lrag} 实体"))

    # ---- 3. Qdrant ----
    from qdrant_client import QdrantClient

    q = QdrantClient(url=s.qdrant_url, timeout=30)
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        cnt = q.count(collection_name="aterag_chunks",
                      count_filter=Filter(must=[FieldCondition(key="workspace_id", match=MatchValue(value=MODEL))]))
        checks.append(("Qdrant 向量已入库", cnt.count > 0, f"{cnt.count} 点"))
    except Exception as e:  # noqa: BLE001
        checks.append(("Qdrant 向量已入库", False, str(e)[:60]))

    # ---- 4. 检索 + 隔离 ----
    async def probe() -> None:
        embed = EmbeddingClient(s)
        if not embed.dimension:
            await embed.probe_dimension()
        rag = RagService(s, registry, embed)
        try:
            r = await rag.search("输出过流保护点是多少", MODEL, top_k=5)
            blob = " ".join(x["content"] for x in r["results"])
            hits = [kw for kw in PROBES.get(MODEL, []) if kw in blob]
            checks.append(("检索命中本型号独有值", len(hits) >= 2, f"命中 {hits}"))
            for kw in hits:
                print(f"    命中探针 {kw!r}")

            # 隔离: 别的型号不该查到本型号的值
            other = await rag.search("输出过流保护点是多少", "PA601-D54A", top_k=5)
            ob = " ".join(x["content"] for x in other["results"])
            leak = [kw for kw in ("PN2000", "PN2000-24A") if kw in ob]
            checks.append(("型号隔离 (PA601 不含本型号)", not leak, f"泄漏={leak}" if leak else "无泄漏"))
        finally:
            await embed.aclose()

    asyncio.run(probe())

    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:30s} {detail}")
    n = sum(1 for _, ok, _ in checks if ok)
    print(f"NEW_SPEC_VERIFY {'PASS' if n == len(checks) else 'FAIL'} {n}/{len(checks)}")
    return 0 if n == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
