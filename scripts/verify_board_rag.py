"""板卡侧同步后验证: 走板卡上运行中的 RAG (MCP), 不在本机复算.

前提: deploy_mcp_board.py 已完成, 且板卡已重跑入库
      (实体抽取规则已变更, 不重跑则 RAG 里是旧数据)。

验证项 (全部经 http://<board>:8080/mcp 的 tools/call):
  1. extract_test_conditions: 章节关键字"功能/性能要求"过滤 + 输入/输出条件
  2. 两路输出电流 (SR-1203) 经 RAG 实体查询
  3. 输入电压分档 (SR-1204 备注) 经条件抽取
  4. 110Vac 落在哪一档, 与两路额定值是否自洽
  5. search_requirements 交叉验证 (检索路径与抽取路径结论一致)

用法:
  $env:BOARD_SSH_USER='rpdzkj'; $env:BOARD_SSH_PASSWORD='<口令>'
  .venv\\Scripts\\python.exe scripts/verify_board_rag.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_MCP_HOST", "192.168.5.24")
PORT = os.getenv("BOARD_MCP_PORT", "8080")
URL = f"http://{HOST}:{PORT}/mcp"
INPUT_VAC = 110.0

PASS, FAIL = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" | {detail}" if detail else ""))


def rpc(method: str, params: dict | None, sid: str | None) -> tuple[str, str | None]:
    payload: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:  # noqa: S310 固定内网地址
        return r.read().decode("utf-8", "replace"), r.headers.get("Mcp-Session-Id")


def call_tool(sid: str, name: str, args: dict) -> dict:
    body, _ = rpc("tools/call", {"name": name, "arguments": args}, sid)
    for line in body.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if "error" in payload:
                raise RuntimeError(f"{name} RPC 错误: {payload['error']}")
            return json.loads(payload["result"]["content"][0]["text"])
    raise RuntimeError(f"{name} 无响应内容")


def main() -> int:
    print(f"=== 板卡 RAG 验证 -> {URL} ===")
    try:
        _, sid = rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "board-verify", "version": "1"},
            },
            None,
        )
    except (urllib.error.URLError, OSError) as e:
        print(f"[FAIL] 无法连接板卡 MCP ({type(e).__name__}: {e})")
        print("       检查: 板卡是否在线 / systemctl status aterag-mcp")
        return 1
    assert sid
    print("MCP 会话建立成功\n")

    # ---------- 1. 章节关键字过滤 + 输入/输出条件 ----------
    print("=== 1. extract_test_conditions (章节关键字: 功能/性能要求) ===")
    ex = call_tool(sid, "extract_test_conditions", {"model_id": "PA601-D54A"})
    check("1-未报 error", "error" not in ex, ex.get("error", ""))
    sel = ex.get("selection") or {}
    check(
        "1-命中章节标题",
        sel.get("matched_headings") == ["4.3 功能/性能要求"],
        str(sel.get("matched_headings")),
    )
    check(
        "1-章节前缀 4.3", sel.get("section_prefixes") == ["4.3"], str(sel.get("section_prefixes"))
    )
    conds = ex.get("conditions") or []
    check("1-条件数 > 0", len(conds) > 0, f"{len(conds)} 条")
    subsecs = sorted({c["section_path"] for c in conds})
    check("1-覆盖 4.3 全部子章", len(subsecs) == 7, str(subsecs))
    check("1-无 4.2/4.4 越界", not any(s.startswith(("4.2", "4.4")) for s in subsecs))
    with_in = sum(1 for c in conds if c["input_conditions"])
    with_out = sum(1 for c in conds if c["output_conditions"])
    check("1-含输入条件(激励)", with_in > 0, f"{with_in}/{len(conds)}")
    check("1-含输出条件(响应)", with_out > 0, f"{with_out}/{len(conds)}")

    st = ex.get("stats") or {}
    print(f"    统计: 留{st.get('kept')} 剔{st.get('excluded')} 待审{st.get('needs_review')}")

    # ---------- 2. 两路输出电流 (经 RAG 实体查询) ----------
    print("\n=== 2. 两路输出电流 (query_parameters, 检索路径) ===")
    q = call_tool(
        sid,
        "query_parameters",
        {
            "query": "PA601-D54A 输出电流 满载 两路",
            "model_id": "PA601-D54A",
            "section_path": "4.3.2",
        },
    )
    blob = json.dumps(q, ensure_ascii=False)
    check("2-检索到 -54V 轨 11.1A", "11.1" in blob)
    check("2-检索到 3.45V 轨 0.1A", "0.1" in blob)
    check("2-两轨电压均出现", "-54" in blob and "3.45" in blob)

    # 以抽取路径为准 (结构化), 检索路径作交叉验证
    cur = [c for c in conds if c["title"] == "输出电流" and c.get("rail")]
    rated: dict[str, float] = {
        c["rail"]: float(c["limits"]["max"]) for c in cur if c["limits"].get("max") is not None
    }
    check("2-抽取侧两轨额定电流一致", set(rated) == {"-54V", "3.45V"}, str(rated))
    check("2-检索与抽取结论一致", str(rated.get("-54V")) in blob and "11.1" in blob, str(rated))

    # ---------- 3. 输入电压分档 ----------
    print("\n=== 3. 输入电压分档 (SR-1204 备注) ===")
    pw = next((c for c in conds if c["title"] == "输出功率"), None)
    check("3-输出功率条目存在", pw is not None)
    tier = [
        i
        for i in (pw["input_conditions"] if pw else [])
        if (i.get("value") or {}).get("tier_power")
    ]
    check("3-切出两个输入档", len(tier) == 2, "; ".join(i["text"] for i in tier))
    hit = [t for t in tier if t["value"]["value"] <= INPUT_VAC <= t["value"]["value2"]]
    power_110 = hit[0]["value"]["tier_power"] if hit else None
    check("3-110Vac 命中档位", power_110 is not None, f"{power_110}W" if power_110 else "未命中")

    # ---------- 4. 自洽性 ----------
    print("\n=== 4. 110Vac 满载自洽性 (P=V*I) ===")
    per_rail = {
        rail: round(v * amps, 2)
        for rail, amps, v in (
            ("-54V", 54.0, rated.get("-54V", 0)),
            ("3.45V", 3.45, rated.get("3.45V", 0)),
        )
    }
    total = round(sum(per_rail.values()), 2)
    for rail, p in per_rail.items():
        print(f"    {rail}: {rated.get(rail)}A -> {p}W")
    print(f"    合计 {total}W  vs  110Vac 档 {power_110}W")
    over = power_110 is not None and total > power_110
    check(
        "4-两轨额定之和超出该档功率上限(矛盾如实暴露)",
        over,
        f"{total}W > {power_110}W" if over else "未超出",
    )
    if over:
        budget = power_110 - per_rail.get("3.45V", 0.0)
        i54 = round(budget / 54.0, 3)
        print(f"    -> 110Vac 满载时 -54V 轨受功率档封顶: {budget}W / 54V = {i54}A")
        check(
            "4-该轨实际电流 < 额定 11.1A",
            i54 < rated.get("-54V", 0),
            f"{i54}A < {rated.get('-54V')}A",
        )

    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n===== {len(results) - n_fail}/{len(results)} passed =====")
    if over:
        print("\n[!] 板卡 RAG 返回的数据本身即暴露规格书冲突: 两轨额定值之和 > 该输入档功率上限。")
        print("    系统如实报告, 未自行取舍 —— 需需求方澄清后修正规格书或标注适用条件。")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
