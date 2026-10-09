"""检索层测试: pgvector + BM25 + RRF 的不变量。

重点是几条**已经真实发生过**的坑 —— 每条断言钉住那个坑现在不复现。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(src: str) -> subprocess.CompletedProcess[str]:
    """在**独立子进程**里跑, 因为本测试要操纵 sys.modules / sys.meta_path,
    在 pytest 进程里做会污染其它用例。"""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(src)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# 核心不变量: 不依赖已移除的向量库, 整条链仍能 import
# ---------------------------------------------------------------------------


def test_entrypoints_import_without_qdrant_client() -> None:
    """模块级 import 向量库客户端就是破损态。

    板卡上没有那个服务, 而 ``rag/service.py`` 与 ``ingest/pipeline.py``
    曾在模块级 import 它 —— 于是连 import 都过不去, ingest 根本起不来。
    这里用 meta_path finder 把它挡掉, 模拟板卡现状。
    """
    proc = _run(
        """
        import sys
        sys.path.insert(0, "src")

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name == "qdrant_client" or name.startswith("qdrant_client."):
                    raise ImportError("blocked: " + name)
                return None

        sys.meta_path.insert(0, Block())
        for mod in ("aterag.config", "aterag.retrieval.hybrid",
                    "aterag.ingest.pipeline", "aterag.rag.service",
                    "aterag.mcp_server.server"):
            __import__(mod)
        print("OK")
        """
    )
    assert proc.returncode == 0, f"无 qdrant 环境下 import 失败:\n{proc.stdout}\n{proc.stderr}"
    assert "OK" in proc.stdout


def test_qdrant_import_only_appears_inside_functions() -> None:
    """qdrant 的 import 语句必须在**函数体内**, 不能在模块顶层。

    这是上一条的结构性保证: 靠「import 出现在缩进块里」来判定, 比逐个
    文件人工核对可靠。
    """
    import ast

    offenders: list[str] = []
    for rel in ("src/aterag/ingest/pipeline.py", "src/aterag/rag/service.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        # 只看**模块体**里的 import(statement 直接挂在 Module.body 上)。
        # 用 ast.walk 会把函数体内的也捞出来 —— 而那些恰恰是允许的。
        # 直接比 Module.body: 想在函数外藏一个 import 已经很困难了。
        for node in tree.body:
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for n in names:
                if n.startswith("qdrant"):
                    offenders.append(f"{rel}:{node.lineno} {n}")
    assert not offenders, f"模块顶层仍有 qdrant import: {offenders}"


# ---------------------------------------------------------------------------
# 融合形态与 rag/service.py 保持一致 (ADR-014 决策 2)
# ---------------------------------------------------------------------------


def test_hybrid_exposes_all_three_primitives() -> None:
    """向量 + BM25 + RRF 三件套都在 hybrid 里, pipeline 只做再导出。"""
    from aterag.ingest import pipeline
    from aterag.retrieval import hybrid

    for name in ("vector_search", "bm25_search", "rrf_fuse", "tag_layers"):
        assert hasattr(hybrid, name), f"hybrid 缺 {name}"
    # pipeline 的再导出必须指向 hybrid, 不能是各自一份实现
    assert pipeline.bm25_search is hybrid.bm25_search
    assert pipeline.rrf_fuse is hybrid.rrf_fuse


def test_bm25_uses_pg_textsearch() -> None:
    """BM25 必须走 pg_textsearch 的 ``<@> to_bm25query(...)``。

    board 上 ``pg_textsearch`` + ``zhparser`` 已装(实测 1.4.0 / 2.4), 中文
    分词靠 ``public.chinese`` 配置。改回别的全文索引不会报错, 只会让中文
    召回悄悄变差 —— 所以这条要显式钉住。
    """
    import ast
    import inspect

    from aterag.retrieval import hybrid

    src = inspect.getsource(hybrid.bm25_search)
    assert "to_bm25query" in src
    assert "idx_aterag_chunks_bm25" in src
    # 判「有没有 import 向量库」要看 AST, 不能用文本匹配 —— docstring 里
    # 提到它是正常的说明性文字, 文本匹配会误报。
    tree = ast.parse(textwrap.dedent(src))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [n for n in imported if n.startswith("qdrant")], imported


def test_vector_search_score_is_cosine_like() -> None:
    """``1 - (embedding <=> q)`` 让 pgvector 分数落在 0~1 的余弦量纲。

    ``rrf_fuse`` 在同一 content 有多路命中时靠 ``score`` 挑代表项; 量纲不一致
    时这个比较就没意义了(虽然 RRF 主分数是排名算的, 但代表项仍会挑错)。
    """
    import inspect

    from aterag.retrieval import hybrid

    src = inspect.getsource(hybrid.vector_search)
    assert "1 - (embedding <=> %s::vector) AS score" in src


def test_tag_layers_marks_unknown_workspace_as_unregistered() -> None:
    """未注册 workspace 的命中必须标 unregistered, 不能标 model。

    标成 "model" 会让混进来的命中被当成型号事实, 污染溯源分层与隔离判断
    —— 这条是从原 rag/service.py 原样搬来的, 不许在重构中丢掉。
    """
    from aterag.retrieval.hybrid import UNREGISTERED_LAYER, tag_layers

    hits = [{"workspace_id": "PA601-D54A"}, {"workspace_id": "不认识的工作区"}]
    out = tag_layers(hits, {"PA601-D54A": "model"})
    assert out[0]["layer"] == "model"
    assert out[1]["layer"] == UNREGISTERED_LAYER == "unregistered"


# ---------------------------------------------------------------------------
# chunk_key: 内容寻址的幂等主键
# ---------------------------------------------------------------------------


def test_chunk_uid_is_deterministic_and_content_addressed() -> None:
    """同内容永远同 key(重导幂等); 内容变了 key 必须变。"""
    from aterag.retrieval.hybrid import chunk_uid

    a = chunk_uid("WS", "4.3.3", "内容")
    b = chunk_uid("WS", "4.3.3", "内容")
    c = chunk_uid("WS", "4.3.3", "内容改了")
    d = chunk_uid("WS2", "4.3.3", "内容")
    assert a == b, "同输入算出不同 key -> 重导会产生重复行"
    assert a != c, "内容变了 key 没变 -> 旧内容查不到也删不掉"
    assert a != d, "workspace 没参与 key -> 跨 workspace 撞车"


def test_chunk_uid_does_not_conflate_adjacent_fields() -> None:
    """字段拼接不能有歧义: 否则 ("AB","C") 与 ("A","BC") 会算出同一个 key。"""
    from aterag.retrieval.hybrid import chunk_uid

    assert chunk_uid("AB", "C", "x") != chunk_uid("A", "BC", "x")


def test_ensure_vector_schema_rejects_unknown_dimension() -> None:
    """dim<=0 必须报错, 不能建零维向量列。

    建出来会让后续写入才报错, 而报错点离根因(维度没探明)很远。
    """
    from aterag.retrieval.hybrid import ensure_vector_schema

    with pytest.raises(ValueError, match="维度"):
        ensure_vector_schema("postgresql://u:p@127.0.0.1:1/nodb", 0)


# ---------------------------------------------------------------------------
# 默认后端
# ---------------------------------------------------------------------------


def test_pgvector_is_the_only_backend() -> None:
    """后端只有 pgvector —— ADR-002「全栈统一 PostgreSQL」。

    ADR-014 的中间态(legacy qdrant 显式 opt-in)已随 W3 验收完成而删除:
    Settings 不再有 `retrieval_backend` 与 `qdrant_*` 字段, 第二套向量存储
    从配置层就构造不出来。
    """
    import inspect

    from aterag.config import Settings

    sig = inspect.signature(Settings)
    dead = [p for p in sig.parameters if "qdrant" in p.lower() or p == "retrieval_backend"]
    assert not dead, f"Settings 残留 legacy 字段: {dead}"
