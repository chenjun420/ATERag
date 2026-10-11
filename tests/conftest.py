"""pytest 会话级前置。

**为什么需要**: 干净 checkout 上跑测试时有三类环境前提, 以前全靠「开发者本地
恰好有 ``.env`` 和 ``.venv``」支撑, CI 上一个都没有:

1. ``Settings`` 必填四项(``llm_base`` / ``llm_model`` / ``embed_base`` /
   ``embed_model``)。缺任何一项, 任何走到抽取的测试都会抛
   ``ValidationError: Field required`` —— 实测 ``test_template_drift.py`` 有 3 条
   在 CI 上因此报错, 而在本地因为有 ``.env`` 一路绿灯。填的是**不可路由的假值**:
   这些测试要么不调模型, 要么在更外层就 mock 掉了。
2. 真实型号的数据(PA601 规格书与 blocks 侧车)按 ``.gitignore`` 不入库, 所以走
   :mod:`scripts.fixture_blocks` 从已入库的自造夹具现场生成。
3. 解释器路径。测试里曾写死 ``.venv/Scripts/python.exe``(Windows 布局), 在
   ubuntu CI 上必然找不到。

第 1 条是这三条里唯一会**假绿**的: 本地有 ``.env`` 时门禁通过, CI 没有时门禁
报错 —— 同一份代码两个结论。所以这里显式补齐, 而不是让每个测试各自设一遍。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# scripts/ 里有 fixture_blocks 与 table_schema_report 等模块, 测试按名字导入它们。
_SCRIPTS = ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

#: Settings 的必填项, 填不可路由的假值。键是小写 —— pydantic-settings 会把环境
#: 变量名映射成小写字段, 而 ``.env`` 里写的是大写, 直接设大写不会生效。
_SETTINGS_STUBS = {
    "llm_base": "http://127.0.0.1:1/v1",
    "llm_model": "stub",
    "embed_base": "http://127.0.0.1:1",
    "embed_model": "stub",
    "postgres_dsn": "postgresql://stub:stub@127.0.0.1:1/stub",
}


def _seed_settings_env() -> list[str]:
    """补齐缺失的 Settings 变量, 返回实际补了哪些(供自检断言)。"""
    filled: list[str] = []
    for k, v in _SETTINGS_STUBS.items():
        if not (os.environ.get(k) or os.environ.get(k.upper())):
            os.environ[k] = v
            filled.append(k)
    return filled


SEEDED_SETTINGS = _seed_settings_env()

# ---------------------------------------------------------------------------
# blocks 侧车指向自造夹具现生成的那一份
# ---------------------------------------------------------------------------
#
# ``Settings.blocks_dir`` 默认是 ``rag_storage/blocks``, 而整个目录被
# .gitignore 排除 —— 干净 checkout 上不存在。走默认路径的调用方
# (``extract_test_conditions`` / MCP 的 ``get_coverage_summary`` 等)于是全部
# ModelNotIngested。实测曾让 9 个测试模块的 46 条测试在 CI 上报错。
#
# **只在真实侧车目录不存在时才改指向夹具现生成的那份。** 无条件改会反过来打断
# 本机: 开发机上 ``rag_storage/blocks/PA601-D54A.jsonl`` 是有的, 被挪走后
# ``test_eid_variant_scheme`` 与 ``test_multi_model_extraction`` 反而全红 ——
# 拿「CI 能跑」换「本机跑不了」是净负收益。真实数据在, 就用它。
#
# 必须在第一次 get_settings() 之前设好 —— 它是 lru_cache 的, 之后再改无效。
# conftest 在各测试模块之前导入, 所以时机成立。
#
# 夹具侧车里只有 PN1000-48A / PN2000-24A: PA601 规格书属客户知识产权, 不入库。
import fixture_blocks  # noqa: E402

_REAL_BLOCKS = ROOT / "rag_storage" / "blocks"
if not _REAL_BLOCKS.is_dir():
    _fixture_dir = str(fixture_blocks.shared_dir())
    os.environ["blocks_dir"] = _fixture_dir
    # 大写的也要设: 读 .env 的那套用的是大写名, 两边都设才不挑环境
    os.environ["BLOCKS_DIR"] = _fixture_dir
