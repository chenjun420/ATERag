"""向板卡 MCP 服务提问 (通用问答 CLI).

用法:
    .venv\\Scripts\\python.exe scripts\\ask_board.py "110伏输入满载下输出电流多少A"
    .venv\\Scripts\\python.exe scripts\\ask_board.py --param 输出电流
    .venv\\Scripts\\python.exe scripts\\ask_board.py --calc output_power --given output_voltage=54 --given rated_current=11.1
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_SSH_HOST", "192.168.5.24")
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
                "clientInfo": {"name": "aterag-ask", "version": "0.1.0"},
            },
        }
    )
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    if len(sys.argv) > 1 and sys.argv[1] == "--param":
        out = tool(
            "query_parameters",
            {"query": sys.argv[2], "model_id": sys.argv[3] if len(sys.argv) > 3 else "PA601-D54A"},
        )
        print(json.dumps(out, ensure_ascii=False, indent=2)[:4000])
        return 0

    if len(sys.argv) > 2 and sys.argv[1] == "--calc":
        given = {}
        for a in sys.argv[3:]:
            if a.startswith("--"):
                continue
            k, _, v = a.partition("=")
            given[k] = float(v)
        out = tool("calculate", {"formula_type": sys.argv[2], "given": given})
        print(json.dumps(out, ensure_ascii=False, indent=2)[:4000])
        return 0

    q = " ".join(sys.argv[1:]) or "110伏输入满载下输出电流多少A"
    print(f"=== query_parameters: {q} ===")
    print(
        json.dumps(
            tool("query_parameters", {"parameter": q, "model_id": "PA601-D54A"}),
            ensure_ascii=False,
            indent=2,
        )[:3000]
    )
    print(f"\n=== search_requirements: {q} ===")
    d = tool("search_requirements", {"query": q, "model_id": "PA601-D54A", "top_k": 6})
    for r in d.get("results", []):
        print(f"  [{r.get('req_id')}] {r.get('heading')} sec={r.get('section_path')}")
        print("    " + r.get("content", "").replace("\n", " ")[:220])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
