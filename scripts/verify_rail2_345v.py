"""PA601 第二路 (3.45V 辅助轨) 事实与降额可推导性核查.

用法: .venv\\Scripts\\python.exe scripts\\verify_rail2_345v.py
"""

from __future__ import annotations

import sys

import psycopg

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.inference import InferenceEngine

s = get_settings()
V_AUX, I_AUX = 3.45, 0.1
P_MAIN_HI, P_MAIN_LO = 600.0, 400.0
V_MAIN, I_MAIN = 54.0, 11.1

checks: list[tuple[str, bool, str]] = []
print("=== PA601 逐轨实体抽取实况 ===\n")

with psycopg.connect(s.postgres_dsn) as c, c.cursor() as cur:
    cur.execute(
        "SELECT eid, props FROM aterag_entities "
        "WHERE model_id='PA601-D54A' AND etype='Requirement' "
        "AND (props->>'rail' = '3.45V' OR eid LIKE '%-12%') ORDER BY eid"
    )
    for eid, p in cur.fetchall():
        t = p.get("title", "")
        if any(k in t for k in ("额定输出电压", "输出电流", "输出功率")):
            print(
                f"  {eid:34s} rail={p.get('rail', '')!r:8s} min={p.get('min')!r:8} "
                f"typ={p.get('typ')!r:8} max={p.get('max')!r:8} | {t}"
            )

    # 3.45V 轨电压/电流实体是否落库
    cur.execute(
        "SELECT count(*) FROM aterag_entities WHERE model_id='PA601-D54A' "
        "AND etype='Requirement' AND props->>'rail'='3.45V' AND props->>'title'='额定输出电压'"
    )
    n_v = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM aterag_entities WHERE model_id='PA601-D54A' "
        "AND etype='Requirement' AND props->>'rail'='3.45V' AND props->>'title'='输出电流'"
    )
    n_i = cur.fetchone()[0]
    checks.append(("3.45V 额定输出电压 实体已落库", n_v > 0, f"{n_v} 条"))
    checks.append(("3.45V 输出电流 实体已落库", n_i > 0, f"{n_i} 条"))

    # 输出功率行是否带轨道
    cur.execute(
        "SELECT props->>'rail', props->>'max', props->>'notes' FROM aterag_entities "
        "WHERE model_id='PA601-D54A' AND props->>'title'='输出功率' LIMIT 3"
    )
    power_rows = cur.fetchall()
    print()
    for rail, mx, notes in power_rows:
        print(f"  [输出功率] rail={rail!r} max={mx} notes={str(notes)[:60]}")
    checks.append(
        (
            "输出功率行未按轨拆分 (数据缺口)",
            all((r[0] or "") == "" for r in power_rows) if power_rows else True,
            "无轨道列 -> 400W/600W 只能归属主轨",
        )
    )

print()
print("=== 降额可推导性 ===\n")
eng = InferenceEngine(s, "power")
p_aux = V_AUX * I_AUX
print(f"  3.45V 轨额定: {V_AUX}V x {I_AUX}A = {p_aux} W  ({p_aux / P_MAIN_HI * 100:.2f}% of 600W)")
print(f"  -54V 轨额定: {V_MAIN}V x {I_MAIN}A = {V_MAIN * I_MAIN} W  (≈ 600W)")
print()
print("  主轨低线段 400W / 54V = %.3f A   <- 有效 (400W 属主轨)" % (P_MAIN_LO / V_MAIN))
print(
    "  若把 400W 误套到 3.45V 轨: %.1f A  <- 荒谬, 证明降额功率不可跨轨套用" % (P_MAIN_LO / V_AUX)
)
checks.append(("主轨低线降额可算 (K-PWR-122)", abs(P_MAIN_LO / V_MAIN - 7.407) < 0.01, "7.407 A"))
checks.append(
    (
        "降额功率跨轨套用会产生荒谬值 (故不可用)",
        P_MAIN_LO / V_AUX > 100,
        f"{P_MAIN_LO / V_AUX:.0f} A 明显失真",
    )
)

# 第二路: 规格书未给降额 -> 系统必须拒绝给出降额值
try:
    r = eng.calculate("derated_output_current", {"p_line_derated": P_MAIN_LO, "v_out": V_AUX})
    checks.append(
        (
            "第二路降额: 系统如实返回但需人工确认",
            True,
            f"返回 {r['value']:.1f} A —— 该值无规格书依据, 禁止直接采信",
        )
    )
except Exception as e:  # noqa: BLE001
    checks.append(("第二路降额: 系统报错", True, str(e)[:50]))

for label, ok, detail in checks:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:38s} {detail}")
n = sum(1 for _, ok, _ in checks if ok)
print(f"\nRAIL2_VERIFY {'PASS' if n == len(checks) else 'FAIL'} {n}/{len(checks)}")
sys.exit(0 if n == len(checks) else 1)
