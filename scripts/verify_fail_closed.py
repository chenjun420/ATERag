"""P0 反幻觉修复验证: 兜底值已清除, 缺失事实 fail-closed.

对照验证 (走板卡 MCP 服务态, 而非脚本态):
  1. get_fixture_spec(PA601)  事实齐全 -> 正常计算, 且 tolerance 标记 not_computed
  2. get_fixture_spec(PN2000-24A) 该规格书写的是"输出电压"而非"额定输出电压",
     电压事实抽不到 -> 必须返回 missing_model_facts, 不得回退到任何默认值
  3. optimize_process 不再返回写死的探针寿命数字
  4. calculate(derated_output_current) 低线降额规则可用 (K-PWR-122)
  5. 回归: 换型号时电压不再被伪造成 54V

用法: .venv\\Scripts\\python.exe scripts\\verify_fail_closed.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_SSH_HOST", "192.168.5.25")
BASE = f"http://{HOST}:8080/mcp"
SID: str | None = None
_ID = 0


def _parse(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(body) if body.strip() else {}


def _post(payload: dict) -> dict:
    global SID
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if SID:
        h["mcp-session-id"] = SID
    req = urllib.request.Request(BASE, method="POST", data=json.dumps(payload).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=300) as r:
        body = r.read().decode("utf-8", "replace")
        if "mcp-session-id" in r.headers:
            SID = r.headers["mcp-session-id"]
    return _parse(body)


def tool(name: str, args: dict) -> dict:
    global _ID
    _ID += 1
    r = _post(
        {
            "jsonrpc": "2.0",
            "id": _ID,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    if "error" in r:
        return {"_rpc_error": r["error"]}
    text = r["result"]["content"][0]["text"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


def main() -> int:
    _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "aterag-failclosed", "version": "0.1.0"},
            },
        }
    )
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    checks: list[tuple[str, bool, str]] = []

    # ---- 1. PA601 事实齐全, 应正常计算 ----
    fx = tool("get_fixture_spec", {"model_id": "PA601-D54A"})
    facts = fx.get("facts", {}) if isinstance(fx, dict) else {}
    v, i = facts.get("voltage"), facts.get("current")
    checks.append(("PA601 电压取自规格书 (54V)", v == 54.0, f"voltage={v}"))
    checks.append(("PA601 电流取自规格书 (11.1A)", i == 11.1, f"current={i}"))
    checks.append(
        (
            "主轨为动态选取 (-54V)",
            facts.get("main_rail") == "-54V",
            f"main_rail={facts.get('main_rail')}",
        )
    )
    checks.append(
        (
            "事实附溯源 req_id",
            bool(facts.get("_provenance", {}).get("current_-54V")),
            str(facts.get("_provenance", {}).get("current_-54V")),
        )
    )

    # ---- 2. 公差链不再返回写死的 [0.3, 0.3] ----
    tol = fx.get("tolerance", {})
    checks.append(
        (
            "公差链不返回伪造数值",
            tol.get("status") == "not_computed"
            and "0.3" not in json.dumps(tol, ensure_ascii=False),
            json.dumps(tol, ensure_ascii=False)[:70],
        )
    )

    # ---- 3. PN2000-24A 缺"额定输出电压" -> 必须 fail-closed ----
    fx2 = tool("get_fixture_spec", {"model_id": "PN2000-24A"})
    is_fc = isinstance(fx2, dict) and fx2.get("error") == "missing_model_facts"
    checks.append(
        (
            "缺事实时 fail-closed 报错",
            is_fc,
            fx2.get("error", json.dumps(fx2, ensure_ascii=False)[:60]),
        )
    )
    if is_fc:
        checks.append(
            ("报错列明缺失项", "voltage" in fx2.get("missing", []), str(fx2.get("missing")))
        )
        checks.append(("报错给出排查线索", "aterag_entities" in fx2.get("hint", ""), "含 SQL 提示"))
        blob = json.dumps(fx2, ensure_ascii=False)
        checks.append(("未泄露任何默认电压值", "54" not in blob and "48" not in blob, "无伪造数值"))

    # ---- 4. optimize_process 不再返回写死探针寿命 ----
    op = tool("optimize_process", {"model_id": "PA601-D54A"})
    pl = op.get("probe_life", {}) if isinstance(op, dict) else {}
    checks.append(
        (
            "探针寿命无伪造默认值",
            pl.get("status") == "not_computed",
            json.dumps(pl, ensure_ascii=False)[:60],
        )
    )

    # ---- 5. 低线降额规则可用 (K-PWR-122) ----
    cal = tool(
        "calculate",
        {"formula_type": "derated_output_current", "inputs": {"p_line_derated": 400, "v_out": 54}},
    )
    val = cal.get("value")
    checks.append(
        (
            "低线降额 400W/54V 可计算",
            isinstance(val, (int, float)) and abs(val - 400 / 54) < 1e-6,
            f"{val} [{cal.get('rule_id')}]",
        )
    )

    # ---- 6. 缺输入不兜底 ----
    cal2 = tool("calculate", {"formula_type": "derated_output_current", "inputs": {"v_out": 54}})
    checks.append(
        ("缺输入报错而非兜底", cal2.get("error") == "calculation_failed", str(cal2.get("error")))
    )

    print("=== P0 反幻觉修复验证 (板卡 MCP 服务态) ===")
    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:32s} {detail}")
    n = sum(1 for _, ok, _ in checks if ok)
    print(f"FAILCLOSED_VERIFY {'PASS' if n == len(checks) else 'FAIL'} {n}/{len(checks)}")
    return 0 if n == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
