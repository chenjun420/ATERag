"""chunk 写入与向量 schema 的两个真实缺陷 (板卡 §4.6 回填时暴露)。

## 1. pgvector 的 atttypmod 被读错 -> **每次 ingest 都静默清空全表向量**

``hybrid.ensure_vector_schema`` 曾按「typmod = 维度 + VARHDRSZ」读维度。实测
(pgvector 0.8.6, 板卡 ``power_specs``): ``vector(3)`` -> typmod **3**、
``vector(1024)`` -> typmod **1024**, 就是维度本身。于是 1024 维的列被读成 1020,
判定「维度不一致」-> ``DROP COLUMN embedding`` -> **整张表的向量清零**。

杀伤面比看上去大: ``aterag_chunks`` 是**所有 workspace 共享的单表**, 所以清掉
的不是「每型号百级」而是全库向量 —— 而那个「重建代价可忽略」的论证前提是错的。
回填 PA601 时就是这么把 ``_domain_power`` 的 131 个向量清掉的。

## 2. ``save_chunks_rows`` 不写 chunk_key -> 同一份内容落两行

它纯 INSERT 且不写 ``chunk_key``, 而 ``save_chunk_vectors`` 按 ``chunk_key``
upsert。前者的行 key 为 NULL, ``ON CONFLICT`` 匹配不到 -> 实测 PA601 从 106 行
变成 212 行(106 行既无 key 也无向量)。而且 NULL 不等于 NULL, 按 key 去重的
``DELETE ... USING`` 也清不掉, 只能手工删。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

HYBRID = ROOT / "src/aterag/retrieval/hybrid.py"
PIPELINE = ROOT / "src/aterag/ingest/pipeline.py"


def _code_only(path: Path) -> str:
    import ast as _ast

    src = path.read_text(encoding="utf-8")
    tree = _ast.parse(src)
    drop: set[int] = set()
    for node in _ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            drop |= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return "\n".join(
        line
        for i, line in enumerate(src.splitlines(), 1)
        if i not in drop and not line.lstrip().startswith("#")
    )


class TestTypmodIsNotOffset:
    def test_no_subtract_four_on_atttypmod(self) -> None:
        """atttypmod 就是维度 —— 再减 4 就是这个静默清库的 bug。"""
        code = _code_only(HYBRID)
        assert "atttypmod - 4" not in code, "仍在按「typmod = 维度 + 4」读维度"
        assert "row[0] - 4" not in code

    def test_dimension_is_read_directly(self) -> None:
        code = _code_only(HYBRID)
        assert "cur_dim = row[0] if row and row[0] and row[0] > 0 else None" in code, (
            "维度解析应直接用 typmod, 并把 <=0 视为「无维度限制」"
        )

    def test_document_records_the_measurement(self) -> None:
        """实测证据必须留在代码里 —— 否则下一个人会按旧假设「修」回去。"""
        src = HYBRID.read_text(encoding="utf-8")
        assert "0.8.6" in src, "没记 pgvector 版本"
        assert "vector(1024)" in src, "没记实测样例"


class TestSaveChunksRowsIsIdempotent:
    def test_writes_chunk_key(self) -> None:
        code = _code_only(PIPELINE)
        assert "chunk_key" in code, "save_chunks_rows 不写 chunk_key -> upsert 匹配不到"

    def test_uses_upsert_not_bare_insert(self) -> None:
        """纯 INSERT 让重跑每次多一份 —— 幂等性不能只靠调用方记得先删。"""
        fn = _function_source(PIPELINE, "save_chunks_rows")
        assert "ON CONFLICT" in fn, "save_chunks_rows 必须按 chunk_key upsert"

    def test_key_is_content_addressed(self) -> None:
        fn = _function_source(PIPELINE, "save_chunks_rows")
        assert "chunk_uid" in fn, "key 必须按内容寻址(workspace+section+content)"

    def test_does_not_touch_embedding(self) -> None:
        """它只写行; 向量由 save_chunk_vectors 写 —— 写进来会把无向量的行
        覆盖成有向量, 但更重要的是职责边界: 一个函数只做一件事。"""
        fn = _function_source(PIPELINE, "save_chunks_rows")
        assert "embedding" not in fn, "save_chunks_rows 不该碰向量列"


class TestDedupLogicNotDuplicated:
    def test_dedup_lives_in_one_place(self) -> None:
        """去重 + 建唯一索引收敛到 ensure_chunk_key_column。

        早先 ``hybrid.ensure_vector_schema`` 与 ``pipeline`` 各写一份, 两份必然漂
        —— 实测就漂过一次(那边建了唯一索引, 这边没写 key)。
        """
        assert "ensure_chunk_key_column" in _code_only(PIPELINE)
        assert "ensure_chunk_key_column" in _code_only(HYBRID)

    def test_hybrid_delegates_instead_of_reimplementing(self) -> None:
        code = _code_only(HYBRID)
        assert "DELETE FROM aterag_chunks a USING aterag_chunks b" not in code, (
            "hybrid 里仍有自己一份去重 SQL —— 应委托给 ensure_chunk_key_column"
        )

    def test_ensure_chunk_key_column_dedups_before_unique_index(self) -> None:
        """唯一索引在有重复的行上会失败 —— 先去重再建。"""
        fn = _function_source(PIPELINE, "ensure_chunk_key_column")
        assert fn.index("DELETE") < fn.index("CREATE UNIQUE INDEX"), "必须先删重复再建唯一索引"

    def test_dedup_keeps_the_row_with_vectors(self) -> None:
        """保留 id 最小的一条 —— 向量后写所以 id 更大。

        反过来(留 id 最大)会在有重复时把有向量的行删掉, 那与「去重是为了恢复
        完整性」的目的正好相反。
        """
        fn = _function_source(PIPELINE, "ensure_chunk_key_column")
        assert "a.id > b.id" in fn, "去重应保留 id 最小的一条"


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
    raise AssertionError(f"{path.name} 里找不到函数 {name}")


class TestNoImportCycle:
    def test_pipeline_and_hybrid_import_each_other_safely(self) -> None:
        """hybrid 运行时 import pipeline —— 必须在函数内, 否则成循环。"""
        src = HYBRID.read_text(encoding="utf-8")
        tree = ast.parse(src)
        module_level = [
            n
            for n in tree.body
            if isinstance(n, ast.ImportFrom) and n.module and "pipeline" in n.module
        ]
        assert not module_level, "hybrid 在模块级 import pipeline -> 会成循环 import"
