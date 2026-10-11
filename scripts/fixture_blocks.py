"""从**已入库的自造规格书**现场生成 blocks 侧车。

为什么现场生成, 不把侧车提交进库
---------------------------------
1. **侧车是派生物**。夹具改了侧车不会跟着改, 于是「侧车与规格书同源」这个
   ``table_schema_report`` / ``template_drift`` 门禁赖以成立的前提会被静默
   打破 —— 而且打破之后门禁还是绿的, 因为它读的也是那份过期侧车。
2. **真实型号的侧车是客户数据, 不能入库**。既然真实侧车不能入库, 自造侧车
   也没必要入库: 仓库里出现两种可得性, 下一个人要猜哪个能当基线。
3. **成本可忽略**。自造夹具只有几 KB, 解析一次不到 10ms。

这个做法不是新发明 —— ``scripts/verify_pn2000.py`` 一直在这么干(它从夹具
生成到临时目录, 不依赖 ``rag_storage/``)。本模块把它固化成共用助手, 免得每个
脚本各写一遍。

**PA601 没有夹具, 是刻意的**
---------------------------
PA601 规格书是客户知识产权(``.gitignore`` 排除), 因此它在 CI 与干净 checkout
里都拿不到。想在 CI 验 PA601 只有两条路: 把客户数据放进仓库, 或换自造样本。
本项目选了后者。需要真实 PA601 数据的地方请用本地跑(见
``docs/规格书上传与处理.md``), 并让它在缺数据时**报错**而不是静默通过。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: model_id -> (夹具相对路径, doc_version)
#:
#: 只列**自造**样本。真实型号(PA601-D54A)不在此, 也不该在 —— 见模块 docstring。
FIXTURE_MODELS: dict[str, tuple[str, str]] = {
    "PN1000-48A": ("tests/fixtures/PN1000-48A 迷你规格书.md", "A"),
    "PN2000-24A": ("tests/fixtures/PN2000-24A 定制电源技术规格书.md", "A"),
}


class FixtureModelUnavailable(RuntimeError):
    """要一个没有自造夹具的型号 —— 典型是 PA601。"""


def available(model_id: str) -> bool:
    """该型号是否有已入库的自造夹具。"""
    return model_id in FIXTURE_MODELS


def fixture_path(model_id: str) -> Path:
    """夹具的绝对路径。"""
    try:
        rel, _ = FIXTURE_MODELS[model_id]
    except KeyError:
        raise FixtureModelUnavailable(
            f"{model_id} 没有入库的自造夹具。仓库只保留自造样本 "
            f"(已入库: {', '.join(sorted(FIXTURE_MODELS))}); 真实型号的规格书属"
            f"客户知识产权, 按 .gitignore 不入库 —— CI 与干净 checkout 里拿不到。"
            f" 门禁/测试要跑真实数据请在本地执行。"
        ) from None
    p = ROOT / rel
    if not p.exists():
        raise FixtureModelUnavailable(f"夹具缺失: {p}")
    return p


def doc_version(model_id: str) -> str:
    return FIXTURE_MODELS[model_id][1]


def blocks_dir(model_id: str, cache_root: str | Path | None = None) -> Path:
    """生成该型号的 blocks 侧车, 返回目录。

    **每次都重新生成, 不按文件存在与否跳过。** 曾经写成「文件在就复用」, 结果
    夹具改完侧车还是旧的 —— 而测试照样全绿, 因为断言读到的是旧侧车算出的结果。
    这正是本模块 docstring 里说的「派生物会静默过期」, 缓存把它又请了回来。
    夹具只有几 KB, 重解析不到 10ms, 省这个没有收益。
    """
    if cache_root is None:
        cache_root = Path(tempfile.gettempdir()) / "aterag_fixture_blocks"
    root = Path(cache_root) / model_id
    root.mkdir(parents=True, exist_ok=True)

    if "src" not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from aterag.ingest.markdown_parser import parse_file

    blocks = parse_file(fixture_path(model_id))
    (root / f"{model_id}.jsonl").write_text(
        "\n".join(json.dumps(b.to_dict(), ensure_ascii=False) for b in blocks),
        encoding="utf-8",
    )
    return root


def blocks_path(model_id: str, cache_root: str | Path | None = None) -> Path:
    """该型号侧车的完整路径。"""
    return blocks_dir(model_id, cache_root) / f"{model_id}.jsonl"


def fixture_models() -> list[str]:
    return sorted(FIXTURE_MODELS)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"已入库的自造夹具: {', '.join(fixture_models())}")
    for m in fixture_models():
        p = blocks_path(m)
        n = len([x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()])
        print(f"  {m:<12} {p}  blocks={n}  {p.stat().st_size}B  doc_version={doc_version(m)}")
    try:
        blocks_dir("PA601-D54A")
    except FixtureModelUnavailable as e:
        print(f"\nPA601-D54A 按预期不可用:\n  {e}")
