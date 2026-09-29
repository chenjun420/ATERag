"""摄取管线: 规格书 -> 三存储 (LightRAG 图谱 / Qdrant 过滤索引 / PG 实体+BM25).

- ingest-spec: 型号文档 -> {model_id} workspace + Qdrant/PG
- build-domain: 领域知识 -> _domain_{type} workspace (只读共享)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import psycopg
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    KeywordIndexParams,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from aterag.config import Settings
from aterag.ingest.classify import classify_product_type
from aterag.ingest.entity_extract import extract_from_blocks
from aterag.ingest.markdown_parser import (
    Block,
    parse_markdown,
    write_blocks_jsonl,
)
from aterag.models import EmbeddingClient, LLMClient
from aterag.registry import Registry

CHUNK_COLLECTION = "aterag_chunks"


# ---------------- PG 实体/分块存储 ----------------
def ensure_pg_schema(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aterag_entities (
                id serial PRIMARY KEY,
                model_id text NOT NULL,
                etype text NOT NULL,
                eid text NOT NULL,
                props jsonb NOT NULL,
                UNIQUE (model_id, etype, eid)
            )"""
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aterag_chunks (
                id serial PRIMARY KEY,
                workspace_id text NOT NULL,
                layer text NOT NULL,
                section_path text DEFAULT '',
                heading text DEFAULT '',
                category text DEFAULT '',
                priority text DEFAULT '',
                rail text DEFAULT '',
                req_id text DEFAULT '',
                content text NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aterag_entities_model ON aterag_entities (model_id, etype)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aterag_chunks_ws ON aterag_chunks (workspace_id)"
        )
        # BM25 全文索引 (pg_textsearch + zhparser 中文配置; 已存在则跳过)
        try:
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_aterag_chunks_bm25
                ON aterag_chunks USING bm25 (content)
                WITH (text_config = 'public.chinese')
                """
            )
        except Exception:  # noqa: BLE001  # 索引已存在/扩展不可用时降级为 LIKE 检索
            conn.rollback()
        conn.commit()


def save_entities(dsn: str, model_id: str, entities: list) -> int:
    with psycopg.connect(dsn) as conn:
        for e in entities:
            conn.execute(
                """
                INSERT INTO aterag_entities (model_id, etype, eid, props)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (model_id, etype, eid)
                DO UPDATE SET props = EXCLUDED.props
                """,
                (model_id, e.etype, e.eid, json.dumps(e.props, ensure_ascii=False)),
            )
        conn.commit()
    return len(entities)


def delete_workspace_chunks(dsn: str, workspace_id: str) -> int:
    with psycopg.connect(dsn) as conn:
        cur = conn.execute("DELETE FROM aterag_chunks WHERE workspace_id = %s", (workspace_id,))
        conn.commit()
        return cur.rowcount


def save_chunks_rows(dsn: str, rows: list[dict]) -> int:
    with psycopg.connect(dsn) as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO aterag_chunks
                    (workspace_id, layer, section_path, heading, category,
                     priority, rail, req_id, content)
                VALUES (%(workspace_id)s, %(layer)s, %(section_path)s, %(heading)s,
                        %(category)s, %(priority)s, %(rail)s, %(req_id)s, %(content)s)
                """,
                r,
            )
        conn.commit()
    return len(rows)


# ---------------- Qdrant chunk 索引 ----------------
def ensure_qdrant(client: QdrantClient, dim: int) -> None:
    existing = {c.name for c in client.get_collections().collections}
    if CHUNK_COLLECTION not in existing:
        client.create_collection(
            collection_name=CHUNK_COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
    for field, tenant in (
        ("workspace_id", True),
        ("section_path", False),
        ("category", False),
        ("priority", False),
        ("layer", False),
    ):
        idx = client.get_collection(CHUNK_COLLECTION).payload_schema or {}
        if field not in idx:
            schema = KeywordIndexParams(type=PayloadSchemaType.KEYWORD, is_tenant=tenant)
            client.create_payload_index(
                collection_name=CHUNK_COLLECTION, field_name=field, field_schema=schema
            )


# ---------------- 分块生成 ----------------
_TABLE_SPLIT = 8  # 每个分块包含的表格行数


def blocks_to_chunks(blocks: list[Block], workspace_id: str, layer: str) -> list[dict]:
    """块 -> 行级分块: 表格按行分组携带元数据, 正文按块保留。"""
    chunks: list[dict] = []
    seq = 0

    def _meta(b: Block, extra: dict | None = None) -> dict:
        base = {
            "workspace_id": workspace_id,
            "layer": layer,
            "section_path": b.section_path,
            "heading": b.heading,
            "category": "protection" if b.section_path.startswith("4.3.3") else "",
            "priority": "",
            "rail": "",
            "req_id": "",
            "content": "",
        }
        base.update(extra or {})
        return base

    for b in blocks:
        # 正文
        if b.text.strip():
            chunks.append(_meta(b, {"content": b.text}))
            seq += 1
        # 表格: 整表携带全元数据, 按行分组
        for table in b.tables:
            if not table:
                continue
            header = table[0]
            body = table[1:] if len(table) > 1 else []
            for i in range(0, len(body), _TABLE_SPLIT):
                group = body[i : i + _TABLE_SPLIT]
                lines = ["| " + " | ".join(header) + " |"] + [
                    "| " + " | ".join(cells) + " |" for cells in group
                ]
                # 从行内提取 req_id / priority / rail 便于过滤
                req_ids: list[str] = []
                prio = ""
                rail = ""
                for cells in group:
                    joined = " ".join(cells)
                    m = re.search(r"SR-[A-Z0-9-]+", joined)
                    if m:
                        req_ids.append(m.group(0))
                    for c in cells:
                        if c in ("强制", "推荐"):
                            prio = c
                        if re.match(r"^-?\d+(?:\.\d+)?V$", c):
                            rail = c
                chunks.append(
                    _meta(
                        b,
                        {
                            "content": "\n".join(lines),
                            "req_id": req_ids[0] if req_ids else "",
                            "priority": prio,
                            "rail": rail,
                        },
                    )
                )
                seq += 1
    return chunks


# ---------------- LightRAG 接线 ----------------
def build_lightrag(
    settings: Settings,
    workspace: str,
    embed: EmbeddingClient,
    llm: LLMClient,
    dim: int,
):
    import os

    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc

    # 守卫: workspace 必须已归一化。LightRAG merge 阶段以 {graph_name} 不带引号拼接 AGE
    # 标识符, PostgreSQL 会把大写折叠成小写, 导致图谱写入失败但 KV/向量层已落库 ——
    # 表现为"入库 status=failed 但分块/实体向量残留"。此处直接失败, 不产生半份脏数据。
    if workspace != lrag_workspace(workspace):
        raise ValueError(
            f"LightRAG workspace 未归一化: {workspace!r} -> 应为 {lrag_workspace(workspace)!r}; "
            f"必须先经 lrag_workspace() 归一化 (全小写, 非字母数字转下划线)"
        )

    # LightRAG PG 后端从 os.environ 读取连接配置
    os.environ.setdefault("POSTGRES_HOST", _host_from_dsn(settings.postgres_dsn))
    os.environ.setdefault("POSTGRES_PORT", "5432")
    os.environ.setdefault("POSTGRES_USER", _user_from_dsn(settings.postgres_dsn))
    os.environ.setdefault("POSTGRES_PASSWORD", _pass_from_dsn(settings.postgres_dsn))
    os.environ.setdefault("POSTGRES_DATABASE", _db_from_dsn(settings.postgres_dsn))
    os.environ["POSTGRES_WORKSPACE"] = workspace

    async def _embed_func(texts: list[str]) -> list[list[float]]:
        import numpy as np

        vecs = await embed.embed(texts)
        return np.array(vecs, dtype=np.float32)  # LightRAG 校验要求 numpy 数组

    async def _llm_func(prompt, system_prompt=None, **kwargs):
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": prompt})
        return await llm.chat(msgs, max_tokens=kwargs.get("max_tokens"))

    return LightRAG(
        working_dir=str(Path(settings.domain_rules_dir).parent / "rag_storage"),
        workspace=workspace,
        kv_storage="PGKVStorage",
        vector_storage="PGVectorStorage",
        graph_storage="PGGraphStorage",
        doc_status_storage="PGDocStatusStorage",
        embedding_func=EmbeddingFunc(
            embedding_dim=dim, func=_embed_func, model_name=settings.embed_model
        ),
        llm_model_func=_llm_func,
        llm_model_name=settings.llm_model,
    )


_DSN_RE = re.compile(r"postgresql://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/([^?]+)")


def lrag_workspace(name: str) -> str:
    """LightRAG workspace 归一化: 全小写 (AGE merge 阶段 schema 限定不带引号,
    PostgreSQL 会折叠大写标识符导致 graph 表查不到)。"""
    return re.sub(r"[^a-z0-9_]", "_", name.lower())


def _user_from_dsn(dsn: str) -> str:
    m = _DSN_RE.match(dsn)
    return m.group(1) if m else "postgres"


def _pass_from_dsn(dsn: str) -> str:
    m = _DSN_RE.match(dsn)
    return m.group(2) if m else ""


def _host_from_dsn(dsn: str) -> str:
    m = _DSN_RE.match(dsn)
    return m.group(3) if m else "localhost"


def _db_from_dsn(dsn: str) -> str:
    m = _DSN_RE.match(dsn)
    return m.group(5) if m else "power_specs"


def entities_to_custom_kg(entities: list, model_id: str) -> dict:
    """本体实体 -> LightRAG custom_kg (确定性图谱注入, 免 LLM 抽取)。"""

    def node(e):
        return {
            "id": f"{e.etype}:{e.eid}",
            "entity_type": e.etype,
            "entity_name": e.eid,
            "content": json.dumps(e.props, ensure_ascii=False),
            "source_id": e.props.get("req_id", "") or e.eid,
            "metadata": {
                "section_path": e.props.get("section_path", ""),
                "model_id": e.props.get("model_id", model_id),
            },
        }

    nodes = [node(e) for e in entities]
    edges = []
    for e in entities:
        if e.etype == "Product":
            continue
        edges.append(
            {
                "source": f"Product:{model_id}",
                "target": f"{e.etype}:{e.eid}",
                "relationship": "has" if e.etype != "Product" else "self",
                "weight": 1.0,
                "source_id": e.eid,
            }
        )
    return {"entities": nodes, "edges": edges, "triplets": []}


# ---------------- 入口流程 ----------------
async def ingest_spec(
    doc_path: str,
    settings: Settings,
    registry: Registry,
    embed: EmbeddingClient,
    llm: LLMClient | None,
) -> dict:
    """导入型号规格书: 解析 -> 分类 -> 实体抽取 -> 三存储。"""
    path = Path(doc_path)
    text = path.read_text(encoding="utf-8")

    # 型号 ID: 从文档首行标题提取 (如 'PA601-D54A 定制电源技术规格书 B')
    m = re.search(r"\b([A-Z]{2,8}\d[A-Z0-9]*(?:-[A-Z0-9]+)+)\b", text[:500])
    if not m:
        raise ValueError(f"无法从文档标题识别型号 ID: {path.name}")
    model_id = m.group(1)

    doc_version = ""
    vm = re.search(r"版本[:：]\s*([A-Z0-9.]+)", text[:800])
    if vm:
        doc_version = vm.group(1)

    domain = await classify_product_type(text, registry, llm)
    registry.register_product(model_id, domain, doc_number="", doc_version=doc_version)

    blocks = parse_markdown(text)
    entities = extract_from_blocks(blocks, model_id, doc_version)
    chunks = blocks_to_chunks(blocks, model_id, layer="model")

    # blocks.jsonl 侧车文件 (规格方案 §4.2.3)
    sidecar_dir = Path("rag_storage/blocks")
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    write_blocks_jsonl(blocks, sidecar_dir / f"{model_id}.jsonl")

    dim = embed.dimension or await embed.probe_dimension()
    ensure_pg_schema(settings.postgres_dsn)
    # 幂等: 重导前清空该型号的旧分块 (实体走 UPSERT)
    delete_workspace_chunks(settings.postgres_dsn, model_id)
    n_ent = save_entities(settings.postgres_dsn, model_id, entities)
    n_chunk = save_chunks_rows(settings.postgres_dsn, chunks)

    qdrant = QdrantClient(url=settings.qdrant_url, timeout=60)
    ensure_qdrant(qdrant, dim)
    vecs = await embed.embed([c["content"] for c in chunks])
    # 确定性 ID: uuid5(命名空间, model:index) — 跨进程幂等, 重导即覆盖
    import uuid

    ns = uuid.UUID("a7e2c9d4-0000-4000-8000-1a7e00000001")
    points = [
        PointStruct(
            id=str(uuid.uuid5(ns, f"{model_id}:{i}")),
            vector=v,
            payload=c | {"doc_version": doc_version},
        )
        for i, (c, v) in enumerate(zip(chunks, vecs))
    ]
    # 分批 upsert
    for i in range(0, len(points), 256):
        qdrant.upsert(collection_name=CHUNK_COLLECTION, points=points[i : i + 256])

    # LightRAG: 确定性实体注入 + 原文入库 (型号 workspace, 归一化小写)
    rag = build_lightrag(settings, lrag_workspace(model_id), embed, llm, dim)
    await rag.initialize_storages()
    try:
        await rag.ainsert_custom_kg(entities_to_custom_kg(entities, model_id))
        await rag.ainsert(text)
    finally:
        await rag.finalize_storages()

    return {
        "model_id": model_id,
        "domain": domain,
        "doc_version": doc_version,
        "blocks": len(blocks),
        "entities": n_ent,
        "chunks": n_chunk,
    }


async def build_domain(
    domain: str,
    settings: Settings,
    registry: Registry,
    embed: EmbeddingClient,
    llm: LLMClient | None,
) -> dict:
    """构建/更新产品类型通用知识库 (_domain_{type} workspace)。"""
    rules_dir = Path(settings.domain_rules_dir) / domain
    narrative_docs: list[str] = []
    rule_docs: list[str] = []
    if rules_dir.exists():
        for f in sorted(rules_dir.glob("*.yaml")):
            rule_docs.append(
                f"# {domain} 领域规则\n\n```yaml\n{f.read_text(encoding='utf-8')}\n```"
            )
        for f in sorted(rules_dir.glob("*.md")):
            narrative_docs.append(f.read_text(encoding="utf-8"))
    if not rule_docs and not narrative_docs:
        return {"domain": domain, "docs": 0, "note": "empty domain kb"}

    workspace = registry.domain_workspace(domain)
    dim = embed.dimension or await embed.probe_dimension()
    # 叙述性文档才进 LightRAG (LLM 抽取); 规则 YAML 走结构化通道, 避免 LLM 对代码块抽取产生噪音
    if narrative_docs:
        rag = build_lightrag(settings, lrag_workspace(workspace), embed, llm, dim)
        await rag.initialize_storages()
        try:
            for d in narrative_docs:
                await rag.ainsert(d)
        finally:
            await rag.finalize_storages()

    # 规则 YAML: 每条规则一个 chunk (细粒度检索); 叙述 MD: 整篇一个 chunk
    delete_workspace_chunks(settings.postgres_dsn, workspace)
    blocks: list[Block] = []
    seq = 0
    for yaml_doc in rule_docs:
        import yaml as _yaml

        data = _yaml.safe_load(yaml_doc.split("```yaml\n", 1)[1].rsplit("```", 1)[0]) or {}
        for rule in data.get("rules", []):
            seq += 1
            parts = [
                f"规则 {rule.get('id', '')}",
                str(rule.get("statement", "")),
            ]
            derive = rule.get("derive") or {}
            if derive.get("expr"):
                parts.append(
                    f"公式: {derive.get('output')} = {derive.get('expr')} 输入: {derive.get('inputs')}"
                )
            src = rule.get("source") or {}
            if src.get("name"):
                parts.append(f"来源: {src.get('name')} {src.get('url', '')}")
            parts.append(f"置信度: {rule.get('confidence', 1.0)}")
            blocks.append(
                Block(
                    chunk_id=f"dom_rule_{seq}",
                    heading=f"{domain}:{rule.get('id', '')}",
                    level=2,
                    text="\n".join(parts),
                )
            )
    for md_doc in narrative_docs:
        seq += 1
        blocks.append(Block(chunk_id=f"dom_doc_{seq}", heading=domain, level=1, text=md_doc))
    chunks = blocks_to_chunks(blocks, workspace, layer="domain")
    ensure_pg_schema(settings.postgres_dsn)
    save_chunks_rows(settings.postgres_dsn, chunks)
    qdrant = QdrantClient(url=settings.qdrant_url, timeout=60)
    ensure_qdrant(qdrant, dim)
    # 幂等: 先清空该 workspace 的旧向量点
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    qdrant.delete(
        collection_name=CHUNK_COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="workspace_id", match=MatchValue(value=workspace))]
        ),
    )
    vecs = await embed.embed([c["content"] for c in chunks])
    # 确定性 ID: uuid5(workspace:index), 跨进程幂等
    import uuid

    ns = uuid.UUID("a7e2c9d4-0000-4000-8000-1a7e00000001")
    points = [
        PointStruct(
            id=str(uuid.uuid5(ns, f"{workspace}:{i}")),
            vector=v,
            payload=c,
        )
        for i, (c, v) in enumerate(zip(chunks, vecs))
    ]
    for i in range(0, len(points), 256):
        qdrant.upsert(collection_name=CHUNK_COLLECTION, points=points[i : i + 256])

    registry.set_domain_populated(domain)
    return {"domain": domain, "rules_chunks": len(rule_docs and blocks), "chunks": len(chunks)}


def bm25_search(
    dsn: str,
    workspaces: list[str],
    query: str,
    top_k: int,
    section_path: str | None = None,
    category: str | None = None,
    priority: str | None = None,
) -> list[dict]:
    """pg_textsearch BM25 检索 (中文配置), 跨 workspace UNION, 支持元数据过滤。"""
    if not workspaces:
        return []
    placeholders = ", ".join(f"'{w}'" for w in workspaces)  # workspace 为内部受控值
    conds = [f"workspace_id IN ({placeholders})"]
    params: list = []
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
    cols = [
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
    ]
    return [dict(zip(cols, r)) for r in rows]


async def qdrant_search(
    client: QdrantClient,
    embed: EmbeddingClient,
    workspaces: list[str],
    query: str,
    top_k: int,
    section_path: str | None = None,
    category: str | None = None,
    priority: str | None = None,
) -> list[dict]:
    """Qdrant 预过滤向量检索 (章节/类别/优先级为 payload 硬过滤)。"""
    must = [FieldCondition(key="workspace_id", match=MatchValue(value=w)) for w in workspaces]
    if section_path:
        must.append(FieldCondition(key="section_path", match=MatchValue(value=section_path)))
    if category and category != "all":
        must.append(FieldCondition(key="category", match=MatchValue(value=category)))
    if priority and priority != "all":
        must.append(FieldCondition(key="priority", match=MatchValue(value=priority)))

    vec = (await embed.embed([query]))[0]
    result = client.query_points(
        collection_name=CHUNK_COLLECTION,
        query=vec,
        query_filter=Filter(must=must),
        limit=top_k,
        with_payload=True,
    )
    out = []
    for p in result.points:
        d = dict(p.payload or {})
        d["score"] = p.score
        out.append(d)
    return out


def rrf_fuse(*ranked_lists: list[dict], k: int = 60, top_k: int = 10) -> list[dict]:
    """RRF 融合多路检索结果 (按 content 去重)。"""
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
