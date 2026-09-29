"""MCP 服务态图检索端到端验证 (确认服务端跑的是修复后的代码).

用法: $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts\\verify_graph_mcp.py
"""
from __future__ import annotations

import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

KEYWORDS = ("过流", "1309")
SID: str | None = None
_ID = 0


def _parse(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(body) if body.strip() else {}


def _headers() -> dict:
    h = {"Accept": "application/json, text/event-stream"}
    if SID:
        h["mcp-session-id"] = SID
    return h


def _post(payload: dict) -> dict:
    global SID
    req = urllib.request.Request(
        "http://127.0.0.1:8080/mcp",
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **_headers()},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        body = r.read().decode("utf-8", "replace")
        if "mcp-session-id" in r.headers:
            SID = r.headers["mcp-session-id"]
    return _parse(body)


def main() -> int:
    _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "aterag-graph", "version": "0.1.0"}}})
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    global _ID
    _ID += 1
    r = _post({"jsonrpc": "2.0", "id": _ID, "method": "tools/call",
               "params": {"name": "search_requirements",
                          "arguments": {"query": "输出过流保护点是多少", "model_id": "PA601-D54A"}}})
    d = json.loads(r["result"]["content"][0]["text"])
    keys = list(d.keys())
    print("search_requirements 返回字段:", keys)

    graph = d.get("graph_results") or []
    print("向量/BM25 命中:", len(d.get("results", [])), "| 图导航命中:", len(graph))
    for g in graph[:2]:
        c = g.get("content", "")
        print(f"  graph hit: source={g.get('source')} len={len(c)} kw={[k for k in KEYWORDS if k in c]}")

    # ---- 判定: 服务端必须已跑修复后的解析逻辑 (不再返回整段截断的无信息片段) ----
    checks: list[tuple[str, bool]] = [
        ("向量/BM25 主检索有结果", len(d.get("results", [])) > 0),
        ("图导航返回独立引用块", len(graph) >= 4),
        ("图导航未落入未解析兜底", all(g.get("source") != "graph-mix-raw" for g in graph)),
        ("图导航块均来自解析后的文档块", all(g.get("source") == "graph-mix" for g in graph)),
        ("图导航块含查询关键字", any(k in g.get("content", "") for g in graph for k in KEYWORDS)),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    npass = sum(1 for _, ok in checks if ok)
    print(f"GRAPH_MCP_VERIFY {'PASS' if npass == len(checks) else 'FAIL'} {npass}/{len(checks)}")
    return 0 if npass == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
