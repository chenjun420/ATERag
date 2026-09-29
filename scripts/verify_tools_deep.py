"""MCP 数据类工具深度验证: query_parameters / get_test_cases / get_fixture_spec."""

import asyncio
import json
import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8080/mcp"


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
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
        r = await self.client.post(
            self.base, json=payload, headers={"Accept": "application/json, text/event-stream"}
        )
        self.sid = r.headers.get("mcp-session-id")
        await self.client.post(
            self.base,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self._headers(),
        )

    def _headers(self):
        h = {"Accept": "application/json, text/event-stream"}
        if self.sid:
            h["mcp-session-id"] = self.sid
        return h

    async def call(self, name, arguments):
        self._id += 1
        r = await self.client.post(
            self.base,
            json={
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers=self._headers(),
        )
        # SSE: 取最后一个可解析且含 result.content 的 data 事件
        best = None
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    j = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                content = j.get("result", {}).get("content")
                if content:
                    best = content[0].get("text", "")
        return best if best is not None else r.text[:500]


async def main():
    m = MCPClient(BASE)
    await m.init()

    print("=== query_parameters 过流保护 (自动识别) ===")
    r = await m.call("query_parameters", {"query": "PA601-D54A 输出过流保护"})
    d = json.loads(r)
    print(
        "model:",
        d.get("model_id"),
        "| protections:",
        len(d.get("protections", [])),
        "| entities:",
        len(d.get("entities", [])),
    )
    for p in d.get("protections", [])[:3]:
        if "过流" in str(p.get("eid", "")):
            print("  ", p["eid"], "trip", p.get("trip_min"), "-", p.get("trip_max"))

    print("=== get_test_cases SR-PA601-D54A-1309 ===")
    r = await m.call(
        "get_test_cases", {"requirement_id": "SR-PA601-D54A-1309", "model_id": "PA601-D54A"}
    )
    d = json.loads(r)
    tc = d.get("test_case") or {}
    print("case:", tc.get("testCaseId"), "| criterion:", tc.get("criterion"))

    print("=== calculate 带型号事实 (自动取 54V/11.1A) ===")
    r = await m.call("calculate", {"formula_type": "power", "model_id": "PA601-D54A"})
    d = json.loads(r)
    print("power:", d.get("value"), "inputs:", d.get("inputs"))

    print("=== get_fixture_spec ===")
    r = await m.call("get_fixture_spec", {"model_id": "PA601-D54A"})
    d = json.loads(r)
    ps = d.get("probe_selection") or {}
    print("facts:", d.get("facts"), "| min_pin_diameter:", ps.get("value"))

    print("=== list_domain_rules ===")
    r = await m.call("list_domain_rules", {})
    d = json.loads(r)
    print("rules:", len(d.get("rules", [])), "| shapes:", d.get("shacl_shapes"))

    await m.client.aclose()


asyncio.run(main())
