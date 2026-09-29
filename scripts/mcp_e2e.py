"""MCP streamable-http 端到端测试客户端.

前置: python -m aterag.mcp_server.server 已启动。
覆盖: 8 业务工具 + 自动识别 + 隔离拒绝 + 健康检查。
"""

import asyncio
import json
import os
import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.getenv("MCP_BASE", "http://127.0.0.1:8080/mcp")
PASS, FAIL = "✅", "❌"
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"{PASS if ok else FAIL} {name}" + (f" | {detail}" if detail else ""))


class MCPClient:
    def __init__(self, base):
        self.base = base
        self.client = httpx.AsyncClient(timeout=120)
        self.sid = None
        self._id = 0

    async def init(self):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "aterag-test", "version": "0.1.0"},
            },
        }
        r = await self.client.post(
            self.base, json=payload, headers={"Accept": "application/json, text/event-stream"}
        )
        r.raise_for_status()
        self.sid = r.headers.get("mcp-session-id")
        # initialized 通知
        await self.client.post(
            self.base,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self._headers(),
        )
        return self._parse(r)

    def _headers(self):
        h = {"Accept": "application/json, text/event-stream"}
        if self.sid:
            h["mcp-session-id"] = self.sid
        return h

    async def list_tools(self):
        r = await self.client.post(
            self.base,
            json={"jsonrpc": "2.0", "id": self._next(), "method": "tools/list"},
            headers=self._headers(),
        )
        return self._parse(r)

    async def call(self, name, arguments):
        r = await self.client.post(
            self.base,
            json={
                "jsonrpc": "2.0",
                "id": self._next(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers=self._headers(),
        )
        return self._parse(r)

    def _next(self):
        self._id += 1
        return self._id

    def _parse(self, r):
        ct = r.headers.get("content-type", "")
        if "text/event-stream" in ct:
            best = None
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        j = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if "result" in j or "error" in j:
                        best = j  # 完整 JSON-RPC 信封 (tools/list 无 content 字段)
            return best if best is not None else {"raw": r.text[:500]}
        return r.json()


async def main():
    mcp = MCPClient(BASE)
    init = await mcp.init()
    check(
        "initialize", "result" in init, init.get("result", {}).get("serverInfo", {}).get("name", "")
    )

    tools = await mcp.list_tools()
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    for t in (
        "search_requirements",
        "query_parameters",
        "calculate",
        "validate_constraints",
        "get_test_cases",
        "get_fixture_spec",
        "search_cases",
        "optimize_process",
        "list_domain_rules",
        "health",
        "list_models",
    ):
        check(f"tool:{t}", t in names)

    # calculate (无型号, 显式输入)
    r = await mcp.call(
        "calculate", {"formula_type": "power", "inputs": {"voltage": 54, "current": 11.1}}
    )
    txt = json.dumps(r, ensure_ascii=False)
    check("calculate: 599.4", "599.4" in txt)

    # search_requirements 自动识别型号
    r = await mcp.call(
        "search_requirements", {"query": "PA601-D54A 输出过流保护点", "section_path": "4.3.3"}
    )
    txt = json.dumps(r, ensure_ascii=False)
    check("search_requirements: 命中", "PA601-D54A" in txt)

    # 隔离拒绝
    r = await mcp.call("search_requirements", {"query": "输出电流", "model_id": "NO-SUCH-1"})
    check("隔离-未注册拒绝", "unknown_model" in json.dumps(r, ensure_ascii=False))

    # health
    r = await mcp.call("health", {})
    check(
        "health 调用",
        "ok" in json.dumps(r, ensure_ascii=False)
        or "postgres" in json.dumps(r, ensure_ascii=False),
    )

    await mcp.client.aclose()
    n_fail = sum(1 for _, ok in results if not ok)
    print(f"\n===== {len(results) - n_fail}/{len(results)} passed =====")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
