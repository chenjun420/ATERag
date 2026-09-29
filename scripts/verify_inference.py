"""推理引擎验证 (纯本地, 无网络)."""
import os
import sys

sys.path.insert(0, "src")

os.environ.setdefault("POSTGRES_DSN", "postgresql://x:x@127.0.0.1/x")
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:6333")
os.environ.setdefault("LLM_BASE", "http://x/v1")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBED_BASE", "http://x")
os.environ.setdefault("EMBED_MODEL", "x")

from aterag.config import get_settings
from aterag.inference import InferenceEngine

s = get_settings()
eng = InferenceEngine(s, "power", model_facts={"voltage": 54, "current": 11.1, "input_power": 644})
print("rules:", len(eng.rules), "shapes:", len(eng.shapes))

cases = [
    ("power", None),
    ("efficiency", None),
    ("fixture_precision", {"param_tolerance": 0.3}),
    ("tolerance", {"components": [0.3, 0.4]}),
    ("probe_selection", {"current": 11.1}),
    ("temp_rise", {"power_loss": 45, "thermal_resistance": 0.5}),
    ("availability", {"mtbf": 500000, "mttr": 0.5}),
    ("channel_count", {"throughput": 1000, "test_time": 120, "available_time": 86400}),
    ("cap_life_factor", {"rated_temp": 105, "operating_temp": 65}),
]
for t, inp in cases:
    r = eng.calculate(t, inp)
    val = r["value"]
    if isinstance(val, float):
        val = round(val, 4)
    print(f"{t:20s} = {val} [{r['rule_id']} layer={r['domain_layer']}]")

t0 = eng.all_traces()[0]
print("trace:", t0["decision_id"][:8], t0["premises"])

# SHACL 验证: 构造违规数据 (保护点 < 恢复点 + 回差)
viol_ttl = """
@prefix ps: <https://aterag.example.org/power#> .
ps:p1 a ps:Protection ;
    ps:tripValue 12 ;
    ps:recoveryValue 11 ;
    ps:hysteresis 3 .
"""
v = eng.validate(viol_ttl)
print("SHACL violation detected:", not v["conforms"], "| shapes:", v["shapes_count"])

ok_ttl = """
@prefix ps: <https://aterag.example.org/power#> .
ps:p1 a ps:Protection ;
    ps:tripValue 15 ;
    ps:recoveryValue 11 ;
    ps:hysteresis 3 .
"""
v2 = eng.validate(ok_ttl)
print("SHACL conforms on valid data:", v2["conforms"])
