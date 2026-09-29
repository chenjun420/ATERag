"""MCP 端到端验证新增规则可经 calculate 工具调用: python scripts/verify_new_rules_mcp.py"""

from __future__ import annotations

import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

CASES = [
    (
        "output_ripple",
        {"ripple_current": 0.62, "switching_frequency": 150000, "capacitance": 0.00022, "esr": 0.1},
    ),
    ("min_hysteresis", {"setpoint": 12, "noise_pp": 0.3}),
    ("rectified_bus_voltage", {"vac_rms": 220}),
    ("ntc_resistance", {"r25": 10000, "b_coeff": 3950, "temperature_k": 323.15}),
    ("max_y_capacitance", {"i_leak": 0.00025, "freq": 60, "v_ac": 264}),
    ("line_regulation_pct", {"v_low_line": 47.9, "v_high_line": 48.2, "v_nominal": 48}),
    (
        "required_bulk_capacitance",
        {"output_power": 120, "hold_up_time": 0.02, "efficiency": 0.9, "v_start": 120, "v_end": 80},
    ),
    ("ovp_trip_voltage", {"setpoint": 24, "track_ratio": 1.1}),
    ("total_thermal_resistance", {"rth_jc": 0.5, "rth_cs": 0.3, "rth_sa": 0.7}),
    ("required_resolution", {"spec_tolerance": 0.3, "process_variation": 0.5}),
]


SID: str | None = None
_ID = 0


def _headers() -> dict:
    h = {"Accept": "application/json, text/event-stream"}
    if SID:
        h["mcp-session-id"] = SID
    return h


def _parse(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    if not body.strip():
        return {}  # 通知类请求无响应体
    return json.loads(body)


def post(payload: dict, headers: dict) -> tuple[dict, dict]:
    global SID
    req = urllib.request.Request(
        "http://127.0.0.1:8080/mcp",
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read().decode("utf-8", "replace")
        hdrs = dict(r.headers)
    if "mcp-session-id" in hdrs:
        SID = hdrs["mcp-session-id"]
    return _parse(body), hdrs


def init() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "aterag-newrules", "version": "0.1.0"},
        },
    }
    post(payload, {"Accept": "application/json, text/event-stream"})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, _headers())


def call(name: str, args: dict) -> dict:
    global _ID
    _ID += 1
    payload = {
        "jsonrpc": "2.0",
        "id": _ID,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    r, _ = post(payload, _headers())
    return r


def main() -> int:
    init()
    fails = 0
    for ft, inp in CASES:
        try:
            r = call("calculate", {"formula_type": ft, "inputs": inp})
            d = json.loads(r["result"]["content"][0]["text"])
            print(f"  OK   {ft:26s} = {d['value']:.8g}  [{d['rule_id']}] conf={d['confidence']}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  FAIL {ft:26s} {type(e).__name__}: {e}")
    print(f"MCP_NEW_RULES {len(CASES) - fails}/{len(CASES)}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
