"""板卡 MCP 服务态新型号可查性验证 (确认服务已加载新注册表).

用法: $env:BOARD_SSH_PASSWORD='xxx'; .venv\\Scripts\\python.exe scripts\\verify_mcp_new_model.py PN2000-24A
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_SSH_HOST", "192.168.5.25")
BASE = f"http://{HOST}:8080/mcp"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "PN2000-24A"
# 该型号独有探针 (来自其规格书: 过流 22~30A, 遥测 1501, 信号 PWOK)
PROBES = ["22.0", "30.0", "PWOK", "过流"]

SID: str | None = None
_ID = 0


def _parse(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(body) if body.strip() else {}


def _post(payload: dict) -> dict:
    global SID
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if SID:
        headers["mcp-session-id"] = SID
    req = urllib.request.Request(
        BASE, method="POST", data=json.dumps(payload).encode(), headers=headers
    )
    with urllib.request.urlopen(req, timeout=180) as r:
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
                "clientInfo": {"name": "aterag-newmodel", "version": "0.1.0"},
            },
        }
    )
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    checks: list[tuple[str, bool, str]] = []

    models = tool("list_models", {})
    prods = models.get("products", {})
    checks.append(("list_models 含新型号", MODEL in prods, f"products={sorted(prods)}"))

    # 免传 model_id, 靠查询文本自动识别 -> 证明注册表已生效
    d = tool("search_requirements", {"query": f"{MODEL} 的输出过流保护点是多少", "top_k": 5})
    checks.append(("自动识别新型号", d.get("model_id") == MODEL, f"model_id={d.get('model_id')}"))
    blob = " ".join(x.get("content", "") for x in d.get("results", []))
    hits = [k for k in PROBES if k in blob]
    checks.append(("检索命中本型号独有值", len(hits) >= 2, f"命中 {hits}"))

    # 判据生成: get_test_cases 收的是 requirement_id (非自由文本), 应给出 22.0~30.0 A 区间
    tc = tool("get_test_cases", {"requirement_id": f"SR-{MODEL}-1309", "model_id": MODEL})
    tcb = json.dumps(tc, ensure_ascii=False)
    checks.append(
        (
            "判据含保护区间 22~30A",
            "22.0" in tcb and "30.0" in tcb,
            f"22.0={'22.0' in tcb} 30.0={'30.0' in tcb}",
        )
    )

    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:28s} {detail}")
    n = sum(1 for _, ok, _ in checks if ok)
    print(f"MCP_NEW_MODEL {'PASS' if n == len(checks) else 'FAIL'} {n}/{len(checks)}")
    return 0 if n == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
