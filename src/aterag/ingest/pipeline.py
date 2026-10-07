"""摄取管线: 规格书 -> 单一 PostgreSQL 底座 (pgvector 预过滤 / pg_textsearch BM25).

- ingest-spec: 型号文档 -> {model_id} workspace + pgvector/BM25 + aterag_entities
- build-domain: 领域知识 -> _domain_{type} workspace (只读共享)

实体到本体节点的映射在 ``aterag.kg.entities``; 本模块只管落库与向量索引。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import psycopg

from aterag.config import Settings
from aterag.ingest.classify import classify_product_type
from aterag.ingest.entity_extract import extract_from_blocks
from aterag.ingest.markdown_parser import (
    Block,
    parse_markdown,
    write_blocks_jsonl,
)
from aterag.ingest.table_schema import load_registry
from aterag.models import EmbeddingClient, LLMClient
from aterag.registry import Registry
from aterag.retrieval import hybrid
from aterag.retrieval.hybrid import chunk_uid

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
    """实体入库。

    先删该型号同类型的旧实体再全量插入, 而不是逐条 UPSERT: UPSERT 只能覆盖
    同名行, 抽取规则变更导致 eid 变化时 (如档位后缀 '#'->'@') 旧行会永久残留,
    造成同一 req_id 同时存在新旧两代记录 —— 实测 PA601 重入库后 PG 380 条
    而抽取只有 201 条, 差的就是历史遗留。
    """
    with psycopg.connect(dsn) as conn:
        etypes = sorted({e.etype for e in entities})
        conn.execute(
            "DELETE FROM aterag_entities WHERE model_id = %s AND etype = ANY(%s)",
            (model_id, etypes),
        )
        # psycopg3 的 Connection 无 executemany (那是 DB-API 2.0 的可选扩展),
        # 统一走 execute 逐条 —— 实测 231 条实体在 2s 内完成, 无需批量化。
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
    """只写 chunk 行 (不写向量)。

    **必须同时写 ``chunk_key`` 并按它 upsert** —— 早先这里只 INSERT 且不写
    ``chunk_key``, 而 ``save_chunk_vectors`` 是按 ``chunk_key`` upsert 的。两者
    连用时前者的行 ``chunk_key IS NULL``, upsert 的 ON CONFLICT 匹配不到, 于是
    **同一份内容落了两行**(板卡实测 PA601-D54A 106 -> 212 行, 其中 106 行既无
    key 也无向量)。NULL 不等于 NULL, 所以按 key 去重的
    ``DELETE ... USING`` 也清不掉它们 —— 脏数据只能手工删。

    upsert 而非 INSERT 也让重跑真正幂等: 调用方不必先删整个 workspace。
    """
    with psycopg.connect(dsn) as conn:
        ensure_chunk_key_column(conn)
        for r in rows:
            key = r.get("chunk_key") or chunk_uid(
                r["workspace_id"], r.get("section_path") or "", r["content"]
            )
            conn.execute(
                """
                INSERT INTO aterag_chunks
                    (chunk_key, workspace_id, layer, section_path, heading, category,
                     priority, rail, req_id, content)
                VALUES (%(chunk_key)s, %(workspace_id)s, %(layer)s, %(section_path)s,
                        %(heading)s, %(category)s, %(priority)s, %(rail)s, %(req_id)s,
                        %(content)s)
                ON CONFLICT (chunk_key) DO UPDATE SET
                    layer       = EXCLUDED.layer,
                    section_path= EXCLUDED.section_path,
                    heading     = EXCLUDED.heading,
                    category    = EXCLUDED.category,
                    priority    = EXCLUDED.priority,
                    rail        = EXCLUDED.rail,
                    req_id      = EXCLUDED.req_id,
                    content     = EXCLUDED.content
                """,
                {**r, "chunk_key": key},
            )
        conn.commit()
    return len(rows)


def ensure_chunk_key_column(conn) -> None:
    """``chunk_key`` 列 + 唯一索引 (幂等)。

    早先这个列由 :func:`hybrid.ensure_vector_schema` 顺带建, 于是「只写行不写向量」
    的路径(:func:`save_chunks_rows`)在列还不存在时写不了 key。这里独立出来, 让
    两条路径对列的依赖各自成立 —— 否则又变成一处「先跑谁」隐式约定。

    建唯一索引前先清历史重复行: 老数据里可能已有同 key 多行(见
    :func:`save_chunks_rows` 的说明), 保留 id 最小的一条 —— 有向量那条, 因为
    upsert 时向量后写、id 更大。
    """
    conn.execute("ALTER TABLE aterag_chunks ADD COLUMN IF NOT EXISTS chunk_key text")
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


# ---------------- legacy Qdrant 后端 (ADR-014 待删, 默认不走) ----------------
def _legacy_qdrant(settings: Settings):
    """惰性构造 Qdrant 客户端。

    **必须惰性**: 模块级 ``from qdrant_client import ...`` 就是 ADR-014:37
    禁止的破损态 —— 板卡上 Qdrant 已经不存在, 留着模块级 import 会让整个
    ingest 在 import 期就炸。改成惰性后, 没装 qdrant 也不影响默认路径。
    """
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:  # pragma: no cover - 仅 legacy 分支
        raise RuntimeError(
            "retrieval_backend=qdrant 需要安装 qdrant-client。默认路径是 pgvector, "
            "不需要 Qdrant(ADR-014: 单一 PostgreSQL 存储底座)。"
        ) from exc
    return QdrantClient(url=settings.qdrant_url, timeout=60)


def _ensure_qdrant_legacy(client, dim: int) -> None:
    """legacy 集合/索引初始化。搬运自原实现, 仅 qdrant 后端使用。"""
    from qdrant_client.models import (
        Distance,
        KeywordIndexParams,
        PayloadSchemaType,
        VectorParams,
    )

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
            client.create_payload_index(
                collection_name=CHUNK_COLLECTION,
                field_name=field,
                field_schema=KeywordIndexParams(type=PayloadSchemaType.KEYWORD, is_tenant=tenant),
            )


def _index_chunks(settings: Settings, workspace: str, chunks: list[dict], vectors, *, replace: bool) -> None:
    """把 chunk 与向量落库。默认 pgvector; ``retrieval_backend=qdrant`` 走 legacy。"""
    if settings.retrieval_backend == "qdrant":
        import uuid

        from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

        client = _legacy_qdrant(settings)
        _ensure_qdrant_legacy(client, len(vectors[0]) if len(vectors) else 0)
        if replace:
            client.delete(
                collection_name=CHUNK_COLLECTION,
                points_selector=Filter(
                    must=[FieldCondition(key="workspace_id", match=MatchValue(value=workspace))]
                ),
            )
        ns = uuid.UUID("a7e2c9d4-0000-4000-8000-1a7e00000001")
        points = [
            PointStruct(
                id=str(uuid.uuid5(ns, f"{workspace}:{i}")),
                vector=v,
                payload=c,
            )
            for i, (c, v) in enumerate(zip(chunks, vectors))
        ]
        for i in range(0, len(points), 256):
            client.upsert(collection_name=CHUNK_COLLECTION, points=points[i : i + 256])
        return
    hybrid.save_chunk_vectors(settings.postgres_dsn, chunks, vectors)


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
    entities = extract_from_blocks(
        blocks, model_id, doc_version, registry=load_registry(settings.table_schemas_path)
    )
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

    hybrid.ensure_vector_schema(settings.postgres_dsn, dim)
    vecs = await embed.embed([c["content"] for c in chunks])
    _index_chunks(settings, model_id, chunks, vecs, replace=False)

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
    hybrid.ensure_vector_schema(settings.postgres_dsn, dim)
    # 幂等: 先清空该 workspace 的旧行(含向量), 再整批写入
    delete_workspace_chunks(settings.postgres_dsn, workspace)
    vecs = await embed.embed([c["content"] for c in chunks])
    _index_chunks(settings, workspace, chunks, vecs, replace=True)

    registry.set_domain_populated(domain)
    return {"domain": domain, "rules_chunks": len(rule_docs and blocks), "chunks": len(chunks)}



# 检索原语已搬到 ``retrieval.hybrid``(BM25 与 RRF 本来就是纯 PG, 与 Qdrant 无关;
# 向量那一路从 Qdrant 换成 pgvector)。这里保留同名再导出, 免得砸掉 scripts/ 下
# 直接 import 它们的验证脚本(verify_search / validate_pa601 等)。
async def legacy_vector_search(
    client,
    embed: EmbeddingClient,
    workspaces: list[str],
    query: str,
    top_k: int,
    section_path: str | None = None,
    category: str | None = None,
    priority: str | None = None,
) -> list[dict]:
    """Qdrant 预过滤向量检索 —— ADR-014 待删的 legacy 路径。

    只在 ``retrieval_backend="qdrant"`` 时走。默认路径是
    ``retrieval.hybrid.vector_search``(pgvector), 不需要 Qdrant。
    过滤语义与 pgvector 版逐项一致, 保证换后端不换检索策略。
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

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


bm25_search = hybrid.bm25_search
rrf_fuse = hybrid.rrf_fuse
