"""两层装配检索验证: model + domain.

原为三层(model + domain + common)。2026-10 移除 common 层: `K-CMN-001`
(SI 词头换算)已并入 `domain_rules/power/`, 所以「单位换算」现在应当从
**域层**命中, 不再是独立第三层。
"""

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.models import EmbeddingClient
from aterag.rag.service import RagService
from aterag.registry import Registry


async def main():
    settings = get_settings()
    registry = Registry.load(settings)
    embed = EmbeddingClient(settings)
    if not embed.dimension:
        await embed.probe_dimension()
    rag = RagService(settings, registry, embed)

    # 1) 型号层: 过流保护点 + 章节过滤
    r1 = await rag.search("输出过流保护点", "PA601-D54A", section_path="4.3.3", use_graph=False)
    print(f"[model] hits={len(r1['results'])}")
    for h in r1["results"][:2]:
        print("  ", h["layer"], h["section_path"], h["content"][:60].replace("\n", " "))

    # 2) 域层: 欧姆定律应从 _domain_power 命中
    r2 = await rag.search("欧姆定律 功率公式", "PA601-D54A", use_graph=False)
    layers_hit = {h["layer"] for h in r2["results"]}
    print(f"[domain] layers={layers_hit}")
    dom_hits = [h for h in r2["results"] if h["layer"] == "domain"]
    for h in dom_hits[:2]:
        print("  ", h["content"][:70].replace("\n", " "))

    # 3) 原共享层的用例并入域层: 单位换算现在从 _domain_power 命中
    r3 = await rag.search("单位换算 词头", "PA601-D54A", use_graph=False)
    dom_from_former_common = [h for h in r3["results"] if h["layer"] == "domain"]
    print(f"[domain|formerly-common] hits={len(dom_from_former_common)}")
    for h in dom_from_former_common[:2]:
        print("  ", h["workspace_id"], h["content"][:70].replace("\n", " "))

    await embed.aclose()


asyncio.run(main())
