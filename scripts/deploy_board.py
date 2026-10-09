"""全链路部署到板卡 192.168.5.25: 预检 -> 双型号规格书 -> 领域库 -> Semantica 语义图.

用法: $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts\\deploy_board.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings
from aterag.ingest.pipeline import build_domain
from aterag.models import EmbeddingClient, LLMClient
from aterag.registry import Registry

STEPS: list[tuple[str, str]] = [
    ("板卡存储栈预检", "scripts/board_preflight.py"),
    ("PA601-D54A 规格书导入", "scripts/ingest_pa601.py"),
    ("PN1000-48A 规格书导入", "scripts/ingest_pn1000.py"),
    ("Semantica 语义图同步", "scripts/sync_semantica.py"),
]


def run_step(label: str, script: str, args: list[str] | None = None) -> bool:
    cmd = [sys.executable, script, *(args or [])]
    print(f"\n>>> {label}: {' '.join(cmd[1:])}")
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()][-6:]
    for ln in tail:
        print("    " + ln[:200])
    if proc.returncode != 0:
        err = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()][-6:]
        for ln in err:
            print("    ERR " + ln[:200])
        print(f"    -> FAIL rc={proc.returncode} ({time.time() - t0:.1f}s)")
        return False
    print(f"    -> OK ({time.time() - t0:.1f}s)")
    return True


async def build_domain_layer() -> bool:
    """领域知识库 -> _domain_power (需真实 embedding/LLM, 走 async 管道)。"""
    print("\n>>> 领域知识库构建 -> _domain_power")
    t0 = time.time()
    s = get_settings()
    registry = Registry.load(s)
    llm = LLMClient(s)
    embed = EmbeddingClient(s)
    if not embed.dimension:
        await embed.probe_dimension()
    try:
        for d in ("power",):
            res = await build_domain(d, s, registry, embed, llm)
            print(f"    {d}: {res}")
    finally:
        await llm.aclose()
        await embed.aclose()
    print(f"    -> OK ({time.time() - t0:.1f}s)")
    return True


def main() -> int:
    s = get_settings()
    print(f"=== ATERag 全链路部署 -> {s.postgres_dsn.split('@')[1].split(':')[0]} ===")
    steps: list[tuple[str, str, list[str] | None]] = [(a, b, None) for a, b in STEPS]
    results: list[bool] = []
    for label, script, _ in steps:
        results.append(run_step(label, script))
        if not results[-1]:
            print("\n部署中断: 前置步骤失败")
            return 1
    try:
        results.append(asyncio.run(build_domain_layer()))
    except Exception as e:  # noqa: BLE001
        print(f"    ERR {type(e).__name__}: {e}")
        results.append(False)

    print("\n=== 部署结果 ===")
    for (label, _, _), ok in zip(steps + [("领域知识库构建", "", None)], results):
        print(f"  [{'OK' if ok else 'FAIL'}] {label}")
    ok = all(results)
    print("DEPLOY_BOARD", "PASS" if ok else "FAIL", f"({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
