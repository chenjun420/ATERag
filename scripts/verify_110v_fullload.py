"""经 RAG 链路验证: 110Vac 输入 + 满载 条件下, 两路输出的输出电流.

这不是单条查表, 而是三段规格的组合:
  A 输入条件  4.3.1 输入工作电压范围 / 标称 110Vac
  B 满载定义  4.3.2 输出功率 SR-1204 (备注含 90~176Vac: 400W; 176~286Vac: 600W)
  C 输出电流  4.3.2 输出电流 SR-1203 (分 -54V / 3.45V 两轨)

关键判定: SR-1203 的 11.1A / 0.1A 是"额定轨电流", 规格书未按输入电压分档;
而 SR-1204 的输出功率**按输入电压分档**。因此 110Vac 下功率上限 400W,
若两轨同时跑到额定值 (-54V 11.1A x 54V = 599.4W) 会超出 400W 上限 —— 必须验证
这个矛盾是否真实存在, 而不是把 11.1A 直接当答案。

用法: .venv\\Scripts\\python.exe scripts/verify_110v_fullload.py
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.extract import extract_test_conditions
from aterag.inference import InferenceEngine
from aterag.models import EmbeddingClient
from aterag.rag.service import RagService
from aterag.registry import Registry

PASS, FAIL = "✅", "❌"
results: list[tuple[str, bool, str]] = []
INPUT_VAC = 110.0


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

    # ---------- A. 输入条件 (章节过滤: 4.3.1) ----------
    print("=== A. 输入条件 (RAG 检索, 4.3.1) ===")
    r = await rag.search(
        "输入工作电压范围 110Vac 标称输入电压",
        model_id="PA601-D54A",
        section_path="4.3.1",
        use_graph=False,
    )
    all_in = all(x["section_path"].startswith("4.3.1") for x in r["results"])
    check("A-检索结果全部来自 4.3.1", bool(r["results"]) and all_in, f"hits={len(r['results'])}")
    cont = " ".join(x["content"] for x in r["results"])
    check("A-含 88~290Vac 工作范围", "88" in cont and "290" in cont)
    check("A-含 110Vac 标称", "110" in cont)

    # ---------- B/C. 结构化抽取 (章节过滤: 功能/性能要求) ----------
    print("\n=== B/C. 结构化抽取 (章节关键字: 功能/性能要求) ===")
    ex = extract_test_conditions("PA601-D54A", doc_version="B")
    check(
        "C-命中 4.3 功能/性能要求",
        ex.selection.section_prefixes == ["4.3"],
        str(ex.selection.section_prefixes),
    )

    cur = [c for c in ex.conditions if c.title == "输出电流" and c.rail]
    check(
        "C-两路输出电流各就位", len({c.rail for c in cur}) == 2, str(sorted({c.rail for c in cur}))
    )
    rated: dict[str, float] = {}
    for c in cur:
        mx = c.limits.get("max")
        if mx is not None:
            rated[c.rail] = float(mx)
    check("C-两路满载额定电流已取到", set(rated) == {"-54V", "3.45V"}, str(rated))

    pw = next((c for c in ex.conditions if c.title == "输出功率"), None)
    check("B-输出功率条目存在", pw is not None)
    pw_max = float(pw.limits.get("max") or 0) if pw else 0.0
    print(f"    额定输出功率上限 = {pw_max} W")
    print(f"    备注原文 = {pw.notes[:80] if pw else ''}")

    # ---------- D. 110Vac 落在哪个功率档 ----------
    print("\n=== D. 110Vac 输入对应的功率档 ===")
    import re

    tier_note = pw.notes if pw else ""
    tiers = re.findall(r"(\d+)\s*[~～]\s*(\d+)Vac\s*[:：]\s*(\d+)\s*W", tier_note)
    check("B-备注含输入电压分档", len(tiers) >= 2, str(tiers))
    hit = [t for t in tiers if float(t[0]) <= INPUT_VAC <= float(t[1])]
    power_at_110 = float(hit[0][2]) if hit else pw_max
    check("D-110Vac 落在 90~176Vac 档", bool(hit), f"档位={hit} -> {power_at_110}W")

    # ---------- E. 两轨额定电流在 110Vac 下是否超功率 ----------
    print("\n=== E. 交叉校验: 两轨跑额定值是否超出 110Vac 功率档 ===")
    eng = InferenceEngine(settings, "power", model_facts={})
    # 用域规则 P=V*I 推导, 而不是脚本里手写乘法 —— 口径与推理引擎一致
    per_rail: dict[str, float] = {}
    for rail, amps in rated.items():
        volts = 54.0 if rail == "-54V" else 3.45
        p = eng.calculate("power", {"voltage": volts, "current": amps})
        per_rail[rail] = round(float(p["value"]), 2)
        print(f"    {rail}: {volts}V x {amps}A = {per_rail[rail]}W")
    total = round(sum(per_rail.values()), 2)
    print(f"    两轨合计 = {total}W  vs  110Vac 档上限 {power_at_110}W")

    over = total > power_at_110
    check(
        "E-两轨额定值之和超出 110Vac 功率档 (矛盾已暴露)",
        over,
        f"{total}W > {power_at_110}W" if over else f"{total}W <= {power_at_110}W",
    )

    # ---------- F. 110Vac 满载下 -54V 轨实际可拉的电流 ----------
    print("\n=== F. 110Vac 满载下两轨电流上限 (受功率档约束) ===")
    i35 = rated.get("3.45V", 0.0)
    p35 = round(3.45 * i35, 3)
    budget = power_at_110 - p35
    i54_max = round(budget / 54.0, 3)
    print(f"    3.45V 轨额定 {i35}A -> {p35}W (占 {p35 / power_at_110 * 100:.2f}%)")
    print(f"    剩余功率预算 = {power_at_110} - {p35} = {budget}W")
    print(f"    -54V 轨在 110Vac 满载下可拉电流 = {budget} / 54 = {i54_max}A")
    check(
        "F-54V 轨 110Vac 满载电流 < 额定 11.1A",
        i54_max < rated.get("-54V", 0),
        f"{i54_max}A < {rated.get('-54V')}A",
    )

    print("\n" + "=" * 62)
    print("结论 (仅依据规格书原文, 不引入规格外假设):")
    print("  额定轨电流 (SR-1203, 强制, 与输入电压无关):")
    for rail, a in rated.items():
        print(f"    {rail:>6} 轨: 0 ~ {a} A")
    print(f"  110Vac 输入下输出功率档 (SR-1204): {power_at_110}W")
    print(f"  110Vac 满载时 3.45V 轨: {i35}A ({p35}W)")
    print(f"  110Vac 满载时 -54V 轨可拉至: {i54_max}A (受 {power_at_110}W 约束)")
    if over:
        print("  [!] 规格书自身存在约束冲突: 两轨额定值之和 > 110Vac 档功率上限。")
        print("      即 -54V 轨的 11.1A 只在 176~286Vac (600W 档) 可达;")
        print("      110Vac 满载时该轨实际受 400W 功率档封顶, 需按 i54_max 设定产测期望。")
        print("      这属于规格书缺口, 建议向需求方确认, 不应由系统自行取其一。")

    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n===== {len(results) - n_fail}/{len(results)} passed =====")
    await embed.aclose()
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
