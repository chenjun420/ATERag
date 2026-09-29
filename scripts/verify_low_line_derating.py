"""低线降额规则接线验证 (K-PWR-122 derive + K-PWR-123 SHACL 约束).

用法: .venv\\Scripts\\python.exe scripts\\verify_low_line_derating.py
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
from aterag.inference.rules import formula_type_to_rule_id

checks: list[tuple[str, bool, str]] = []

# ---- 1. formula_type 已注册 ----
rid = formula_type_to_rule_id("derated_output_current")
checks.append(("formula_type 已注册", rid == "K-PWR-122", f"-> {rid}"))

eng = InferenceEngine(get_settings(), "power")

# ---- 2. PA601 实际数据: 110Vac 落 90~176Vac 段, 400W / 54V ----
r = eng.calculate("derated_output_current", {"p_line_derated": 400, "v_out": 54})
val = r["value"]
checks.append(("110Vac 段 400W/54V = 7.407A", abs(val - 400 / 54) < 1e-6, f"{val:.4f} A [{r['rule_id']}]"))
checks.append(("溯源标注 layer", bool(r.get("domain_layer")), str(r.get("domain_layer"))))

# ---- 3. 高压段 600W/54V = 11.1A (与规格书额定一致) ----
r2 = eng.calculate("derated_output_current", {"p_line_derated": 600, "v_out": 54})
checks.append(("高压段 600W/54V = 11.1A (对齐 SR-1203)", abs(r2["value"] - 11.111) < 0.01, f"{r2['value']:.4f} A"))

# ---- 4. 缺输入必须报错, 不得兜底 ----
try:
    eng.calculate("derated_output_current", {"v_out": 54})
    checks.append(("缺 p_line_derated 报错(fail-closed)", False, "竟成功返回 — 存在兜底"))
except (KeyError, ValueError) as e:
    checks.append(("缺 p_line_derated 报错(fail-closed)", True, f"{type(e).__name__}: {str(e)[:50]}"))

# ---- 5. SHACL 约束: 低线判据不得沿用额定值 ----
viol = """
@prefix ps: <https://aterag.example.org/power#> .
@prefix pst: <https://aterag.example.org/power-test#> .
pst:t1 a ps:PowerTest ;
    ps:ratedPowerW 600 ;
    ps:lineDeratedPowerW 400 .
"""
v = eng.validate(viol)
checks.append(("违规数据被检出", not v["conforms"], f"conforms={v['conforms']}"))

ok = """
@prefix ps: <https://aterag.example.org/power#> .
@prefix pst: <https://aterag.example.org/power-test#> .
pst:t2 a ps:PowerTest ;
    ps:ratedPowerW 600 ;
    ps:lineDeratedPowerW 600 .
"""
v2 = eng.validate(ok)
checks.append(("合法数据通过", v2["conforms"], f"conforms={v2['conforms']}"))

print("=== 低线降额规则接线验证 ===")
for label, ok_, detail in checks:
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {label:36s} {detail}")
n = sum(1 for _, ok_, _ in checks if ok_)
print(f"LOWLINE_VERIFY {'PASS' if n == len(checks) else 'FAIL'} {n}/{len(checks)}")
sys.exit(0 if n == len(checks) else 1)
