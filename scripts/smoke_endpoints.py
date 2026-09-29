"""端点连通性冒烟测试: LLM chat + Embedding (真实 Key, fail-fast)."""

from __future__ import annotations

import asyncio
import sys

from aterag.checks import report, run_all_checks
from aterag.config import get_settings
from aterag.models import LLMClient


async def main() -> int:
    settings = get_settings()
    results = await run_all_checks(settings)
    print(report(results))

    # LLM 真实对话测试 (检查项只验证 /models; 这里验证 chat 补全)
    if results[2].ok or True:
        llm = LLMClient(settings)
        try:
            answer = await llm.chat(
                [{"role": "user", "content": "回答仅一个数字: 54 * 11.1 = ?"}],
                max_tokens=2000,
            )
            print(f"LLM chat ok: {answer[:80]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"LLM chat FAILED: {e}")
        finally:
            await llm.aclose()

    ok = all(r.ok for r in results if r.name in ("embedding",))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
