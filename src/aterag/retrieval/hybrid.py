"""检索融合层: pgvector + BM25 + RRF。**这是全系统唯一的检索实现。**

## 为什么是 pgvector + BM25 两路

中文规格书里 ``SR-1203``、``-54V``、``11.1A`` 这类**精确标识符**靠向量
相似度命不中, 必须有 BM25 那一路兜底; 而要跨 workspace 三层
(``[model, _domain_{type}, _common]``)做联合检索与元数据硬过滤, 又需要能
按 ``workspace_id IN (...)`` 下推到存储层的引擎。两条路都齐了, 再用 RRF
按名次融合, 是满足这两条要求的最小组合。

## 决策依据存档 (LightRAG mix 模式的实测结论)

LightRAG 已从依赖中移除(它只用于入库时的实体关系抽取, 而那条路的产物
无人查询 —— 检索全程走本模块)。移除前实测过它的 ``mix`` 模式, 结论记在这里
以免将来重新评估时重踩:

* **不含 BM25**: ``mix`` 做的是三次**向量**检索再做 round-robin 合并 ——
  ``_get_node_data(ll_keywords, ..., entities_vdb)`` +
  ``_get_edge_data(hl_keywords, ..., relationships_vdb)`` +
  ``_get_vector_context(query, chunks_vdb)``。LLM 先抽出
  ``ll_keywords``/``hl_keywords``, 但那些 keyword 拿去**做向量查询**, 不走
  倒排/全文索引; 整包 lightrag 里 ``bm25``/``ts_rank``/``plainto_tsquery``
  零命中。
* **一个进程只服务一个 workspace**: ``LightRAG.workspace`` 是 dataclass
  字段, 构造时冻结; ``LIGHTRAG-WORKSPACE`` 请求头只在 ``/health`` 被消费
  (``lightrag_server.py`` 里 ``get_workspace_from_request`` 的唯一调用点在
  ``get_status`` 内), 而 ``/query`` 用的 ``rag`` 是闭包捕获的单一实例。
  拿不到跨三层 workspace 的联合检索。
* **PG 后端的 workspace 是进程级环境变量**: ``postgres_impl.py`` 里
  ``os.environ["POSTGRES_WORKSPACE"]`` 优先于构造参数(日志原文:
  ``overriding '<self.workspace>/<self.namespace>'``), 所以同进程内多实例
  会互相覆盖。

三条合起来: 满足产测检索要求的只有本模块这条自研路径。

## 融合形态与 rag/service.py 保持一致

ADR-014 决策 2 明确: 「融合算法与原 rag/service.py 保持同一形态(先向量预过滤
再 BM25 再 RRF), 避免『换存储』被顺手换成『换检索策略』」。故本模块的步骤、
过滤语义、层标规则、RRF 参数(k=60, 按 content[:200] 去重) 都照抄原实现,
只把向量那一路的**存储**从 Qdrant 换成 pgvector。

硬过滤语义与原 ``qdrant_search`` 逐项一致: ``workspace_id IN (...)`` 是前提,
``section_path`` / ``category`` / ``priority`` 逐项收紧; 后两者传 ``"all"``
视为不过滤(原实现如此, 这里照抄)。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import psycopg

from aterag.models import EmbeddingClient

#: 未知 workspace 的层标。与 ``rag/service.py`` 的 ``UNREGISTERED_LAYER`` 同义 ——
#: 混入未注册 workspace 的命中会被误当成型号事实, 污染溯源分层与隔离判断。
UNREGISTERED_LAYER = "unregistered"

#: chunk 确定性主键的命名空间。**任意但必须固定** —— 一旦改动, 同一份内容的
#: chunk 会算出不同 key, 重灌时旧行不会被覆盖而变成孤儿。
_CHUNK_NS = uuid.UUID("6f8d5a3e-1c4b-4d2a-9f70-5b3e2c1a8d40")

CHUNK_COLUMNS = (
    "id",
    "workspace_id",
    "layer",
    "section_path",
    "heading",
    "category",
    "priority",
    "rail",
    "req_id",
    "content",
)


def chunk_uid(workspace_id: str, section_path: str, content: str) -> str:
    """chunk 的确定性主键(内容寻址)。

    原 Qdrant 路径用 ``uuid5(命名空间, f"{workspace}:{model}:{index}")``
    (见 DELIVERY-REPORT.md:152「chunk UUID 必须确定」)。这里改成内容寻址:
    同一 workspace + 章节 + 内容永远算出同一个 key, 于是**重复导入是幂等的**
    —— 重灌覆盖而不是追加。原先靠 index 的方案在块顺序变化时会算出新 key,
    旧行就成了既不被查到也不被删的孤儿。
    """
    return str(uuid.uuid5(_CHUNK_NS, f"{workspace_id}\x1f{section_path}\x1f{content}"))


def _vec_literal(vec: Sequence[float]) -> str:
    """把向量格式化成 pgvector 的文本字面量。

    不走 psycopg 的 pgvector adapter —— 那要求额外注册, 而这里 cast 成
    ``::vector`` 就够了, 少一个隐式依赖。
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


# ---------------------------------------------------------------------------
# 表结构
# ---------------------------------------------------------------------------


def ensure_vector_schema(dsn: str, dim: int) -> None:
    """建 chunk 向量列与索引, 并保证已存在的列维度与 ``dim`` 一致。

    维度不一致时**不能**直接 ADD COLUMN(pgvector 会因类型不符报错), 也**不能**
    悄悄留着旧维度的列 —— 那样写入时才报错, 且报错点离根因很远。这里先读
    ``atttypmod`` 拿到实际维度, 不同就重建列(数据量是每型号百级 chunk,
    重建代价可忽略, 而留错维度会让检索静默返回空)。
    """
    if dim <= 0:
        raise ValueError(f"embed 维度未探明 (dim={dim}); 不允许建零维向量列")

    with psycopg.connect(dsn) as conn:
        exists = conn.execute(
            "SELECT to_regclass('public.aterag_chunks') IS NOT NULL"
        ).fetchone()[0]
        if not exists:
            raise RuntimeError(
                "aterag_chunks 表不存在; 先调 ingest.pipeline.ensure_pg_schema(dsn)"
            )

        # 1) 维度探查 + 必要时重建
        cur_dim = None
        if conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='aterag_chunks' AND column_name='embedding'"
        ).fetchone():
            row = conn.execute(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'public.aterag_chunks'::regclass "
                "AND attname = 'embedding' AND NOT attisdropped"
            ).fetchone()
            # vector 的 atttypmod 就是维度; 传统 typmod 是「维度 + 4」
            cur_dim = (row[0] - 4) if row and row[0] and row[0] > 0 else None

        if cur_dim is not None and cur_dim != dim:
            conn.execute("DROP INDEX IF EXISTS idx_aterag_chunks_vec")
            conn.execute("ALTER TABLE aterag_chunks DROP COLUMN embedding")
        if cur_dim != dim:
            conn.execute(
                f"ALTER TABLE aterag_chunks ADD COLUMN embedding vector({dim})"
            )

        # 2) 确定性主键 + 回填 + 唯一索引
        conn.execute(
            "ALTER TABLE aterag_chunks ADD COLUMN IF NOT EXISTS chunk_key text"
        )
        n_missing = conn.execute(
            "SELECT count(*) FROM aterag_chunks WHERE chunk_key IS NULL"
        ).fetchone()[0]
        if n_missing:
            rows = conn.execute(
                "SELECT id, workspace_id, coalesce(section_path, ''), content "
                "FROM aterag_chunks WHERE chunk_key IS NULL"
            ).fetchall()
            for cid, ws, sp, content in rows:
                conn.execute(
                    "UPDATE aterag_chunks SET chunk_key = %s WHERE id = %s",
                    (chunk_uid(ws, sp or "", content), cid),
                )
        # 已有重复(同一内容出现多行)时唯一索引会失败; 保留 id 最小的一条
        conn.execute(
            """
            DELETE FROM aterag_chunks a USING aterag_chunks b
             WHERE a.chunk_key = b.chunk_key AND a.id > b.id
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_aterag_chunks_key "
            "ON aterag_chunks (chunk_key)"
        )

        # 3) 向量索引。HNSW 优于 IVFFlat: 规格书库规模(每型号百级 chunk)
        #    远未触及两者瓶颈, 而 HNSW 不需要先训 centroids。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aterag_chunks_vec "
            "ON aterag_chunks USING hnsw (embedding vector_cosine_ops)"
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def save_chunk_vectors(dsn: str, rows: list[dict], vectors: list[Sequence[float]]) -> int:
    """把 chunk 与其向量一起落库(按 ``chunk_key`` upsert)。

    ``rows`` 与 ``vectors`` 必须等长且顺序对应 —— ``rows`` 是
    ``pipeline.blocks_to_chunks()`` 的输出, ``vectors`` 是对 ``content`` 逐条
    嵌入的结果。
    """
    if len(rows) != len(vectors):
        raise ValueError(f"rows/vectors 不等长: {len(rows)} vs {len(vectors)}")
    if not rows:
        return 0
    with psycopg.connect(dsn) as conn:
        for r, v in zip(rows, vectors):
            key = r.get("chunk_key") or chunk_uid(
                r["workspace_id"], r.get("section_path") or "", r["content"]
            )
            conn.execute(
                """
                INSERT INTO aterag_chunks
                    (chunk_key, workspace_id, layer, section_path, heading, category,
                     priority, rail, req_id, content, embedding)
                VALUES (%(chunk_key)s, %(workspace_id)s, %(layer)s, %(section_path)s,
                        %(heading)s, %(category)s, %(priority)s, %(rail)s, %(req_id)s,
                        %(content)s, %(embedding)s::vector)
                ON CONFLICT (chunk_key) DO UPDATE SET
                    layer       = EXCLUDED.layer,
                    section_path= EXCLUDED.section_path,
                    heading     = EXCLUDED.heading,
                    category    = EXCLUDED.category,
                    priority    = EXCLUDED.priority,
                    rail        = EXCLUDED.rail,
                    req_id      = EXCLUDED.req_id,
                    content     = EXCLUDED.content,
                    embedding   = EXCLUDED.embedding
                """,
                # 全部命名占位符。原实现混用 ``%s::vector``(位置) 与
                # ``%(name)s``(命名) 并试图同时传 dict 与 tuple, 报
                # "Connection.execute() takes from 2 to 3 positional arguments but 4
                # were given" —— psycopg 只接受一个参数序列。向量按 pgvector 的
                # 字面量形式(``[1,2,3]``)传, 配 ``::vector`` 转型。
                {**r, "chunk_key": key, "embedding": _vec_literal(v)},
            )
        conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


async def vector_search(
    dsn: str,
    embed: EmbeddingClient,
    workspaces: list[str],
    query: str,
    top_k: int,
    section_path: str | None = None,
    category: str | None = None,
    priority: str | None = None,
) -> list[dict]:
    """pgvector 余弦预过滤检索 —— 替换原 ``pipeline.qdrant_search``。

    过滤语义逐项照抄原实现(见模块 docstring)。返回的 dict 形状也一致:
    ``score`` 是**余弦相似度**(``1 - 余弦距离``), 与 Qdrant 的 COSINE 打分
    同量纲, 这样 ``rrf_fuse`` 选代表项时的比较才有意义。
    """
    if not workspaces:
        return []
    placeholders = ", ".join(f"'{w}'" for w in workspaces)  # workspace 为内部受控值
    conds = [f"workspace_id IN ({placeholders})", "embedding IS NOT NULL"]
    params: list[Any] = []
    if section_path:
        conds.append("section_path = %s")
        params.append(section_path)
    if category and category != "all":
        conds.append("category = %s")
        params.append(category)
    if priority and priority != "all":
        conds.append("priority = %s")
        params.append(priority)

    vec = (await embed.embed([query]))[0]
    literal = _vec_literal(vec)
    sql = f"""
        SELECT id, workspace_id, layer, section_path, heading, category,
               priority, rail, req_id, content,
               1 - (embedding <=> %s::vector) AS score
        FROM aterag_chunks
        WHERE {' AND '.join(conds)}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    params += [literal, literal, top_k]
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(zip((*CHUNK_COLUMNS, "score"), r))
        d["score"] = float(d["score"])
        out.append(d)
    return out


def bm25_search(
    dsn: str,
    workspaces: list[str],
    query: str,
    top_k: int,
    section_path: str | None = None,
    category: str | None = None,
    priority: str | None = None,
) -> list[dict]:
    """pg_textsearch BM25 检索 (中文配置), 跨 workspace UNION, 支持元数据过滤。

    从 ``ingest.pipeline`` 原样搬来 —— 它本来就是纯 PG, 与 Qdrant 无关,
    搬过来是为了让检索原语都在一处, 而不是留一半在 ingest 一半在 retrieval。
    """
    if not workspaces:
        return []
    placeholders = ", ".join(f"'{w}'" for w in workspaces)  # workspace 为内部受控值
    conds = [f"workspace_id IN ({placeholders})"]
    params: list[Any] = []
    if section_path:
        conds.append("section_path = %s")
        params.append(section_path)
    if category and category != "all":
        conds.append("category = %s")
        params.append(category)
    if priority and priority != "all":
        conds.append("priority = %s")
        params.append(priority)
    where = " AND ".join(conds)
    sql = f"""
        SELECT id, workspace_id, layer, section_path, heading, category,
               priority, rail, req_id, content
        FROM aterag_chunks
        WHERE {where}
        ORDER BY content <@> to_bm25query(%s, 'idx_aterag_chunks_bm25')
        LIMIT %s
    """
    params += [query, top_k]
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(zip(CHUNK_COLUMNS, r)) for r in rows]


def rrf_fuse(*ranked_lists: list[dict], k: int = 60, top_k: int = 10) -> list[dict]:
    """RRF 融合多路检索结果 (按 content 去重)。从 ``ingest.pipeline`` 原样搬来。"""
    scores: dict[str, float] = {}
    best: dict[str, dict] = {}
    for lst in ranked_lists:
        for rank, item in enumerate(lst):
            key = item.get("content", "")[:200]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in best or item.get("score", 0) > best[key].get("score", 0):
                best[key] = item
    ordered = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
    return [best[key] | {"rrf_score": s} for key, s in ordered]


def tag_layers(hits: list[dict], layer_map: dict[str, str]) -> list[dict]:
    """按 workspace -> layer 打层标; 未知 workspace 标 ``unregistered``。

    照抄 ``rag/service.py`` 的规则: 混入未注册 workspace 的命中会被误当成
    型号事实, 污染溯源分层与隔离判断。
    """
    for h in hits:
        h["layer"] = layer_map.get(h.get("workspace_id", ""), UNREGISTERED_LAYER)
    return hits


async def search(
    dsn: str,
    embed: EmbeddingClient,
    workspaces: list[str],
    layer_map: dict[str, str],
    query: str,
    top_k: int = 8,
    section_path: str | None = None,
    category: str | None = None,
    priority: str | None = None,
) -> list[dict]:
    """向量预过滤 -> BM25 -> RRF, 返回 ``rrf_score`` 已设的命中列表。

    ``top_k * 2`` 取每路候选再截断到 ``top_k``: RRF 只看排名, 候选太少会让
    融合失去意义(照抄 ``rag/service.py`` 的取数倍数)。
    """
    vec = await vector_search(
        dsn,
        embed,
        workspaces,
        query,
        top_k=top_k * 2,
        section_path=section_path,
        category=category,
        priority=priority,
    )
    bm = bm25_search(
        dsn,
        workspaces,
        query,
        top_k=top_k * 2,
        section_path=section_path,
        category=category,
        priority=priority,
    )
    return rrf_fuse(tag_layers(vec, layer_map), tag_layers(bm, layer_map), top_k=top_k)
