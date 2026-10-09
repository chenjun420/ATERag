"""全链路端到端验证编排: 依次执行全部验证套件并汇总.

用法: $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts\\verify_all.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


def assert_suite_scripts_exist(suites=None) -> list[str]:
    """返回引用了不存在脚本的套件名列表 (空 = 全部存在)。

    **编排器自己必须先于被编排者被验证。** 曾有一个套件引用了已删除的脚本,
    而 ``subprocess.run`` 抛的 FileNotFoundError 会被宽泛捕获成「该套件失败」——
    看不出真相是**编排器指向一个不存在的文件**。所以这里 fail-closed。

    ``suites=None`` 在函数体里取 ``SUITES`` 而不是在默认参数里 —— 默认参数
    于定义时求值, 而这个函数刻意定义在 ``SUITES`` 之前(否则清单里就得引用
    一个还没定义的东西), 写成 ``suites=SUITES`` 会直接 NameError。
    """
    missing: list[str] = []
    for name, cmd, _expect in SUITES if suites is None else suites:
        for token in cmd:
            if token.startswith("scripts/") and not Path(token).exists():
                missing.append(f"{name} -> {token}")
    if missing:
        raise FileNotFoundError(
            "以下套件引用了不存在的脚本 (编排器的死引用, 不会被任何测试发现):\n  "
            + "\n  ".join(missing)
        )
    return []


# (套件名, 脚本, 期望通过标记的正则)
SUITES: list[tuple[str, list[str], re.Pattern[str]]] = [
    ("纯本地单测", ["-m", "pytest", "tests/", "-q"], re.compile(r"(\d+) passed")),
    (
        "规则自验 (derive + SHACL)",
        ["scripts/rules_selftest.py"],
        re.compile(r"selftest=(\d+)/(\d+)"),
    ),
    (
        "推理引擎 (纯本地)",
        ["scripts/verify_inference.py"],
        re.compile(r"SHACL conforms on valid data: True"),
    ),
    (
        "端点连通 (PG/LLM/Embedding)",
        ["scripts/smoke_endpoints.py"],
        re.compile(r"LLM chat ok"),
    ),
    ("两层装配隔离", ["scripts/verify_layers.py"], re.compile(r"\[domain\] layers=.*domain")),
    ("检索链路 (BM25/向量/RRF)", ["scripts/verify_search.py"], re.compile(r"RRF fused=[1-9]")),
    (
        "图谱导航 (mix 引用解析)",
        ["scripts/verify_graph.py"],
        re.compile(r"GRAPH_VERIFY PASS \d+/\d+"),
    ),
    (
        "图导航 MCP 服务态",
        ["scripts/verify_graph_mcp.py"],
        re.compile(r"GRAPH_MCP_VERIFY PASS \d+/\d+"),
    ),
    ("实体抽取", ["scripts/verify_extract.py"], re.compile(r"EXTRACT_VERIFY PASS \d+/\d+")),
    ("工具深验", ["scripts/verify_tools_deep.py"], re.compile(r"rules: \d+")),
    ("PA601 全量验证", ["scripts/validate_pa601.py"], re.compile(r"(\d+)/(\d+) passed")),
    ("PN1000 隔离验证", ["scripts/verify_pn1000.py"], re.compile(r"PN1000_VERIFY PASS \d+/\d+")),
    ("MCP e2e (13 工具)", ["scripts/mcp_e2e.py"], re.compile(r"(\d+)/(\d+) passed")),
    ("Semantica 语义图回读", ["scripts/verify_semantica.py"], re.compile(r"SEMANTICA_VERIFY PASS")),
    (
        "新规则 MCP 端到端",
        ["scripts/verify_new_rules_mcp.py"],
        re.compile(r"MCP_NEW_RULES \d+/\d+"),
    ),
    ("板卡存储栈预检", ["scripts/board_preflight.py"], re.compile(r"BOARD_PREFLIGHT PASS")),
    (
        "低线降额规则接线",
        ["scripts/verify_low_line_derating.py"],
        re.compile(r"LOWLINE_VERIFY PASS \d+/\d+"),
    ),
    (
        "P0 反幻觉 fail-closed (板卡)",
        ["scripts/verify_fail_closed.py"],
        re.compile(r"FAILCLOSED_VERIFY PASS \d+/\d+"),
    ),
    (
        "硬编码清理回归 (无白名单/不顶格)",
        ["scripts/verify_no_hardcoded.py"],
        re.compile(r"NO_HARDCODED_VERIFY PASS \d+/\d+"),
    ),
    (
        # 模板漂移门禁 (方案 §4.0③⑤)。这是**辅助**工具: 抽取过程本身对模板失配
        # 已 fail-closed, 这里管的是「参数变了但抽取照样跑完」的那一类静默失效。
        # 放在离线段而非板卡段: 它只读配置与 blocks, 不需要 PG。
        "模板漂移门禁 (辅助)",
        ["scripts/template_drift.py", "--model", "PA601-D54A", "--model", "PN1000-48A"],
        re.compile(r"TEMPLATE_DRIFT PASS"),
    ),
]


def main() -> int:
    print("=== ATERag 全链路端到端验证 ===\n")
    assert_suite_scripts_exist()
    rows: list[tuple[str, str, bool, float]] = []
    for name, cmd, expect in SUITES:
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, *cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0 and bool(expect.search(out))
        tail = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
        marker = expect.search(out)
        detail = marker.group(0) if marker else (tail[-1][:80] if tail else "")
        if not ok and proc.returncode != 0:
            detail = f"rc={proc.returncode} " + (tail[-1][:70] if tail else "")
        rows.append((name, detail, ok, time.time() - t0))
        print(f"[{'PASS' if ok else 'FAIL'}] {name:38s} {detail}")

    print("\n=== 汇总 ===")
    npass = sum(1 for r in rows if r[2])
    total_t = sum(r[3] for r in rows)
    for name, detail, ok, dt in rows:
        if not ok:
            print(f"  FAILED: {name} ({detail})")
    print(
        f"VERIFY_ALL {'PASS' if npass == len(rows) else 'FAIL'} {npass}/{len(rows)}  ({total_t:.1f}s)"
    )
    return 0 if npass == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
