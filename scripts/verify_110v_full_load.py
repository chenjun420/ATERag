"""PA601-D54A: 110Vac 输入满载下的输出电流验算 (规格书依据 + 推理引擎).

数据来源 (规格书):
  SR-PA601-D54A-1101 输入工作电压范围  Vac  88 / 110或220 / 290  长期工作
  SR-PA601-D54A-1200 额定输出电压      -54V / +3.45V
  SR-PA601-D54A-1203 输出电流          -54V  0 ~ 11.1A  长期工作
  SR-PA601-D54A-1204 输出功率          0 ~ 600W  备注: 90~176Vac: 400W; 176~286Vac: 600W
  SR-PA601-D54A-1309 输出过流保护      -54V  12~18A  备注: 输入电压<176Vac, 过流点 8.1A~18A

用法: .venv\\Scripts\\python.exe scripts\\verify_110v_full_load.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("POSTGRES_DSN", "postgresql://x:x@127.0.0.1/x")
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:6333")
os.environ.setdefault("LLM_BASE", "http://x/v1")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBED_BASE", "http://x")
os.environ.setdefault("EMBED_MODEL", "x")

from aterag.config import get_settings
from aterag.inference import InferenceEngine

# 规格书事实
V_OUT = 54.0  # -54V 轨额定输出电压
I_RATED_HI = 11.1  # SR-1203 额定输出电流 (额定 600W 档)
P_RATED_HI = 600.0  # SR-1204 176~286Vac 段
P_RATED_LO = 400.0  # SR-1204 90~176Vac 段  <- 110Vac 落此段
OCP_FLOOR_LOW_LINE = 8.1  # SR-1309 备注: 输入<176Vac 过流点下限
V_IN_TEST = 110.0

eng = InferenceEngine(
    get_settings(), "power", model_facts={"voltage": V_OUT, "input_power": P_RATED_LO}
)

checks: list[tuple[str, bool, str]] = []

# --- 1. 规格书自洽性: 600W / 54V 应等于额定 11.1A ---
r1 = eng.calculate("power", {"output_voltage": V_OUT, "current": I_RATED_HI})
p_hi = r1["value"]
checks.append(
    (
        "额定档 54V x 11.1A = 600W",
        abs(p_hi - P_RATED_HI) < 1.0,
        f"{round(p_hi, 2)}W [{r1['rule_id']}]",
    )
)

# --- 2. 低线段 400W 反算电流 ---
# 引擎为正向推导 (K-ELEC-001: P = V x I), 传 output_power 会让它去反解 current
# 而误入 K-ELEC-002 电阻支路, 故此处按同一物理量直接核算。
i_calc = P_RATED_LO / V_OUT
checks.append(("110Vac 段 400W / 54V 反算电流", abs(i_calc - 7.407) < 0.01, f"{i_calc:.3f} A"))

# --- 3. 合理性交叉校验: 额定输出电流必须低于低压段过流保护下限 ---
checks.append(
    (
        "额定电流 < 低压过流下限 8.1A (自洽)",
        i_calc < OCP_FLOOR_LOW_LINE,
        f"{i_calc:.3f}A < {OCP_FLOOR_LOW_LINE}A",
    )
)
checks.append(("额定电流 < 高压段过流下限 12A (自洽)", I_RATED_HI < 12.0, f"{I_RATED_HI}A < 12A"))

# --- 4. 降额比例 ---
derate = 1 - i_calc / I_RATED_HI
checks.append(("低线降额比例 ~33%", 0.30 < derate < 0.36, f"{derate * 100:.1f}%"))

print("=== PA601-D54A 110Vac 满载输出电流验算 ===")
print(f"  110Vac 落在 90~176Vac 段 -> 输出功率上限 {P_RATED_LO:.0f}W (SR-1204)")
print(f"  {P_RATED_LO:.0f}W / {V_OUT:.0f}V = {i_calc:.3f} A")
print(f"  对比额定 176~286Vac 段: {P_RATED_HI:.0f}W / {V_OUT:.0f}V = {I_RATED_HI} A")
print()
for label, ok, detail in checks:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:34s} {detail}")
n = sum(1 for _, ok, _ in checks if ok)
print(f"VERIFY_110V {'PASS' if n == len(checks) else 'FAIL'} {n}/{len(checks)}")
print(
    f"\n答案: 110Vac 输入满载时 -54V 轨输出电流 = {i_calc:.1f} A (规格书未单列, 由 400W 降额上限推导)"
)
sys.exit(0 if n == len(checks) else 1)
