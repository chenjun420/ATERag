"""检索融合层: pgvector + BM25 + RRF。**这是全系统唯一的检索实现。**

## 为什么是 pgvector + BM25 两路

中文规格书里 ``SR-1203``、``-54V``、``11.1A`` 这类**精确标识符**靠向量
相似度命不中, 必须有 BM25 那一路兜底; 而要跨 workspace 三层
(``[model, _domain_{type}]``)做联合检索与元数据硬过滤, 又需要能
按 ``workspace_id IN (...)`` 下推到存储层的引擎。两条路都齐了, 再用 RRF
按名次融合, 是满足这两条要求的最小组合。

## 融合形态

步骤、过滤语义、层标规则、RRF 参数(k=60, 按 ``content[:200]`` 去重) 是
照着 ``rag/service.py`` 的实现复制的 —— 那边才是融合逻辑的所在地, 本模块
只负责把向量那一路的存储换成 pgvector。两边必须保持同一形态, 否则
「换存储」会被顺手换成「换检索策略」。

硬过滤语义: ``workspace_id IN (...)`` 是前提, ``section_path`` /
``category`` / ``priority`` 逐项收紧; 后两者传 ``"all"`` 视为不过滤。
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

    必须是确定值 —— DELIVERY-REPORT.md:152「chunk UUID 必须确定」。
    这里用内容寻址: 同一 workspace + 章节 + 内容永远算出同一个 key, 于是
    **重复导入是幂等的** —— 重灌覆盖而不是追加。靠 index 的方案在块顺序
    变化时会算出新 key,
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
        exists = conn.execute("SELECT to_regclass('public.aterag_chunks') IS NOT NULL").fetchone()[
            0
        ]
        if not exists:
            raise RuntimeError("aterag_chunks 表不存在; 先调 ingest.pipeline.ensure_pg_schema(dsn)")

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
            # pgvector 的 ``atttypmod`` **就是维度本身**。实测 (pgvector 0.8.6,
            # 板卡 power_specs): ``vector(3)`` -> 3、``vector(1024)`` -> 1024,
            # 不是「维度 + VARHDRSZ」。
            #
            # 早先这里写的是 ``typmod - 4`` (按「typmod = 维度 + 4」的旧假设),
            # 于是 1024 维的列被读成 1020 -> 每次 ingest 都判定「维度不一致」->
            # 执行下面的 DROP COLUMN -> **静默清空整张表的向量**(所有 workspace)。
            # 而 ``aterag_chunks`` 是**所有 workspace 共享的单表**, 所以清空的是
            # 全库向量, 不是「每型号百级」—— 那个「重建代价可忽略」的论证前提是错的。
            #
            # typmod 为 0 或负表示「无维度限制的 vector」, 视为未建。
            cur_dim = row[0] if row and row[0] and row[0] > 0 else None

        if cur_dim is not None and cur_dim != dim:
            conn.execute("DROP INDEX IF EXISTS idx_aterag_chunks_vec")
            conn.execute("ALTER TABLE aterag_chunks DROP COLUMN embedding")
        if cur_dim != dim:
            conn.execute(f"ALTER TABLE aterag_chunks ADD COLUMN embedding vector({dim})")

        # 2) 确定性主键 + 回填 + 唯一索引
        # 去重与建索引收敛到 pipeline.ensure_chunk_key_column —— 早先这里与
        # save_chunks_rows 各写一份, 两份必然漂(实测漂过一次)。
        from aterag.ingest.pipeline import ensure_chunk_key_column

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
        ensure_chunk_key_column(conn)

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


def _priority_values(raw: str | None) -> list[str]:
    """等级参数 -> 值列表。

    支持逗号分隔多值(``"强制,推荐"``); ``""``/``None``/``"all"`` 都表示不过滤。
    否定项(``exclude_priority``)用同一个解析器 —— 两处语义都是**值集合**,
    不是布尔开关, 调用方自己决定给哪组词。

    口径(方案 §11.4): ``强制`` / ``推荐`` / ``不要求`` / ``不建议`` 这类等级词
    由 doc_profiles.sieve 在入库前按**整格等值**收口 —— 库里的等级列只会出现
    正向值或空串。排除「不要求/无要求」的负向过滤之所以仍然有存在意义: 历史行
    (规则改前入的库)与人工注记回填可能带来这些值, 检索面要能显式拒收而不是
    靠「碰不到」。
    """
    vals = [v.strip() for v in (raw or "").split(",") if v.strip()]
    return [v for v in vals if v != "all"]


async def vector_search(
    dsn: str,
    embed: EmbeddingClient,
    workspaces: list[str],
    query: str,
    top_k: int,
    section_path: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    exclude_priority: str | None = None,
) -> list[dict]:
    """pgvector 余弦预过滤检索。

    过滤语义: ``workspace_id IN (...)`` 是前提(逐项照抄原实现);
    ``section_path``/``category`` 精确等值; ``priority`` 支持**多值集合**
    (逗号分隔, 命中任一即出现)与 ``exclude_priority`` **负向集合**(命中
    任一即不出现 —— 典型用法: ``"不要求,无要求"`` 排掉无要求等级)。
    返回的 dict 形状一致, ``score`` 是**余弦相似度**(``1 - 余弦距离``)。
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
    prios = _priority_values(priority)
    ex_prios = _priority_values(exclude_priority)
    if prios:
        conds.append("priority = ANY(%s)")
        params.append(prios)
    if ex_prios:
        conds.append("priority <> ALL(%s)")
        params.append(ex_prios)
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
        WHERE {" AND ".join(conds)}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    # SQL 中 %s 出现顺序: 打分向量 -> (section_path/category/priority 过滤) ->
    # 排序向量 -> LIMIT。过滤参数先于本轮追加, 必须插在两个向量占位符之间,
    # 直接 += 会把 '4.3.1' 这类字符串绑到 ::vector 上 (实测炸过)。
    params = [literal, *params, literal, top_k]
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
    exclude_priority: str | None = None,
) -> list[dict]:
    """pg_textsearch BM25 检索 (中文配置), 跨 workspace UNION, 支持元数据过滤。

    从 ``ingest.pipeline`` 原样搬来 —— 搬过来是为了让检索原语都在一处,
    而不是留一半在 ingest 一半在 retrieval。
    ``priority``/``exclude_priority`` 语义与 :func:`vector_search` 一致
    (单值保持向后兼容; 多值/负向集合走 ANY/ALL 数组参数)。
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
    prios = _priority_values(priority)
    ex_prios = _priority_values(exclude_priority)
    if prios:
        conds.append("priority = ANY(%s)")
        params.append(prios)
    if ex_prios:
        conds.append("priority <> ALL(%s)")
        params.append(ex_prios)
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
    exclude_priority: str | None = None,
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
        exclude_priority=exclude_priority,
    )
    bm = bm25_search(
        dsn,
        workspaces,
        query,
        top_k=top_k * 2,
        section_path=section_path,
        category=category,
        priority=priority,
        exclude_priority=exclude_priority,
    )
    return rrf_fuse(tag_layers(vec, layer_map), tag_layers(bm, layer_map), top_k=top_k)
