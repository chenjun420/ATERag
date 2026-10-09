"""新产品规格书导入 CLI (开发机与板卡通用, 复用 ingest_spec 全链路).

用法:
    # 本地 (开发机)
    python scripts/ingest_new_spec.py "D:/docs/PN2000-24A 规格书.md"

    # 板卡 (MCP 服务所在机; 文件须已在板卡上)
    /opt/aterag/.venv/bin/python /opt/aterag/scripts/ingest_new_spec.py /opt/aterag/specs/PN2000-24A.md

自动完成: 型号识别 -> 产品类型分类 -> 注册 -> 解析 -> 实体抽取 ->
PG 实体/分块 + 向量 (可重复执行, 幂等覆盖)。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 所有配置项 (registry_path / domain_rules_dir / working_dir) 都是相对 CWD
# 解析的。板卡上经 SSH 执行时 CWD 可能是 / 或家目录, 必须先切到应用根, 否则:
#   - .env 读不到            -> 配置项全部缺失
#   - registry.yaml 写错位置 -> 新型号注册后"查不到"
#   - domain_rules 找不到    -> 规则库为空
#   - rag_storage 散落他处   -> sidecar 碎片
APP_ROOT = Path(__file__).resolve().parent.parent
os.chdir(APP_ROOT)
sys.path.insert(0, str(APP_ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import Settings
from aterag.ingest.pipeline import ingest_spec
from aterag.models import EmbeddingClient, LLMClient
from aterag.registry import Registry


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    doc = Path(sys.argv[1])
    if not doc.exists():
        print(f"文件不存在: {doc}")
        return 1

    env_file = APP_ROOT / ".env"
    if not env_file.exists():
        print(f"配置文件不存在: {env_file}")
        return 1
    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    registry = Registry.load(settings)
    llm = LLMClient(settings)
    embed = EmbeddingClient(settings)
    if not embed.dimension:
        await embed.probe_dimension()
    print(f"导入: {doc}")
    print(f"embed dim={embed.dimension}")
    try:
        result = await ingest_spec(str(doc), settings, registry, embed, llm)
    except Exception as e:  # noqa: BLE001
        print(f"INGEST_FAILED: {type(e).__name__}: {e}")
        return 1
    finally:
        await llm.aclose()
        await embed.aclose()
    print("INGEST_RESULT:", result)
    print(f"已注册型号: {sorted(registry.products)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
