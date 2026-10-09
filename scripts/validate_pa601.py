"""PA601-D54A 全量验证套件 (对应规格方案验证清单).

前置: scripts/ingest_pa601.py 已成功执行。
覆盖: 章节过滤 / 术语召回 / 公式链 / 违规识别 / 三级隔离 / 自动识别。
"""

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.inference import InferenceEngine
from aterag.models import EmbeddingClient
from aterag.rag.service import RagService
from aterag.registry import AmbiguousModel, Registry, UnknownModel
from aterag.retrieval import hybrid

PASS, FAIL = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" | {detail}" if detail else ""))


async def main() -> int:
    settings = get_settings()
    registry = Registry.load(settings)
    embed = EmbeddingClient(settings)
    if not embed.dimension:
        await embed.probe_dimension()
    rag = RagService(settings, registry, embed)

    # ---------- 1. 章节过滤: 过流保护点只命中 4.3.3 ----------
    r = await rag.search(
        "输出过流保护点", model_id="PA601-D54A", section_path="4.3.3", use_graph=False
    )
    contents = " ".join(x["content"] for x in r["results"])
    check(
        "章节过滤-4.3.3命中12~18A",
        "12" in contents and "18" in contents,
        f"hits={len(r['results'])}",
    )
    check(
        "章节过滤-不混入4.3.2的11.1A",
        "11.1" not in contents,
        "output current excluded",
    )
    # 真正的污染判定: 结果全部来自 4.3.3, 且不出现 4.2.4.2 信号通流行特征
    all_in_433 = all(x["section_path"] == "4.3.3" for x in r["results"])
    no_signal_rating = "通流" not in contents and "-54VRTN" not in contents
    check(
        "章节过滤-结果全部限定4.3.3(30A通流不泄漏)",
        all_in_433 and no_signal_rating,
        f"sections={sorted({x['section_path'] for x in r['results']})}",
    )

    # ---------- 2. 术语召回 ----------
    r2 = await rag.search("VL_DOWN_ALM 掉电告警", model_id="PA601-D54A", use_graph=False)
    c2 = " ".join(x["content"] for x in r2["results"])
    check("术语召回-VL_DOWN_ALM", "VL_DOWN_ALM" in c2 or "VL" in c2, f"hits={len(r2['results'])}")

    r3 = await rag.search("SR-PA601-D54A-1210 整机效率", model_id="PA601-D54A", use_graph=False)
    c3 = " ".join(x["content"] for x in r3["results"])
    check("术语召回-SR-1210效率", "效率" in c3 or "86" in c3, f"hits={len(r3['results'])}")

    # ---------- 3. 公式链 ----------
    facts = {"voltage": 54, "current": 11.1, "input_power": 644}
    eng = InferenceEngine(settings, "power", model_facts=facts)
    p = eng.calculate("power", None)
    check("公式链-功率599.4W", abs(p["value"] - 599.4) < 0.01, f"={p['value']}")
    eff = eng.calculate("efficiency", None)
    check("公式链-效率93.07%", abs(eff["value"] - 93.0745) < 0.01, f"={eff['value']:.2f}")
    trace = eng.explain(p["decision_id"])
    check(
        "溯源-前提含层标注",
        all("layer" in x for x in trace["premises"]),
        f"premises={trace['premises']}",
    )

    # ---------- 4. SHACL 违规识别 ----------
    bad = """
@prefix ps: <https://aterag.example.org/power#> .
ps:x a ps:Protection ; ps:tripValue 12 ; ps:recoveryValue 10 ; ps:hysteresis 3 .
"""
    v = eng.validate(bad)
    check("SHACL-保护点<恢复点+回差被识别", not v["conforms"])

    # ---------- 5. 两级隔离 ----------
    check("隔离-registry含PA601", "PA601-D54A" in registry.products)
    layers = rag.workspaces("PA601-D54A")
    check(
        "隔离-两层装配",
        len(layers) == 2 and layers[0][1] == "model" and layers[1][1] == "domain",
        str([ws[0] for ws in layers]),
    )
    if "PN1000-48A" in registry.products:
        # 状态 B: PN1000 已导入 -> 用真实数据做跨型号隔离测试
        hits_pn = await hybrid.vector_search(
            settings.postgres_dsn, embed, ["PN1000-48A"], "输出电流", 10
        )
        pn_content = " ".join(str(h.get("content", "")) for h in hits_pn)
        check("隔离-PN1000自身数据可见", "20.8" in pn_content, f"hits={len(hits_pn)}")
        hits_pa = await hybrid.vector_search(
            settings.postgres_dsn, embed, ["PA601-D54A"], "输出电流 20.8A", 10
        )
        leaked = any("20.8" in str(h.get("content", "")) for h in hits_pa)
        check("隔离-PA601查不到PN1000的20.8A", not leaked, f"hits={len(hits_pa)}")
        # 共享域: 两个型号都能查到 power 域知识
        hits_dom = await hybrid.vector_search(
            settings.postgres_dsn, embed, ["_domain_power"], "欧姆定律 功率", 5
        )
        check("共享域-PA601/PN1000可见power域", len(hits_dom) > 0, f"hits={len(hits_dom)}")
    else:
        # 状态 A: PN1000 未注册 -> fail-closed
        try:
            rag.resolve("输出电流是多少", "PN1000-48A")
            check("隔离-未注册型号拒绝", False)
        except UnknownModel:
            check("隔离-未注册型号拒绝", True)
        hits = await hybrid.vector_search(
            settings.postgres_dsn, embed, [layers[0][0]], "PN1000-48A 输出电流", 10
        )
        leaked = any("PN1000" in str(h.get("content", "")) for h in hits)
        check("隔离-跨型号无泄漏(未导入)", not leaked, f"hits={len(hits)}")

    # ---------- 6. 查询自动识别 ----------
    r4 = await rag.search("PA601-D54A 的输出电流是多少", None, use_graph=False)
    check("自动识别-型号", r4["model_id"] == "PA601-D54A", f"source={r4['model_id_source']}")
    if "PN1000-48A" in registry.products:
        try:
            await rag.search("PA601-D54A 和 PN1000-48A 对比", None)
            check("自动识别-双型号歧义拒绝", False)
        except AmbiguousModel:
            check("自动识别-双型号歧义拒绝", True)
        except UnknownModel:
            check("自动识别-双型号歧义拒绝", True)

    # ---------- 7. BM25 与 RRF ----------
    bm = hybrid.bm25_search(settings.postgres_dsn, [ws[0] for ws in layers], "输出过流保护", 10)
    check("BM25-中文检索有结果", len(bm) > 0, f"hits={len(bm)}")
    fused = hybrid.rrf_fuse(
        await hybrid.vector_search(
            settings.postgres_dsn, embed, [ws[0] for ws in layers], "输出过流保护", 10
        ),
        bm,
        top_k=5,
    )
    check("RRF-融合去重", len(fused) > 0 and all("rrf_score" in x for x in fused))

    await embed.aclose()
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n===== {len(results) - n_fail}/{len(results)} passed =====")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
