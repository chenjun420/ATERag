"""RAG 检索服务: 两层 workspace 装配 + 双引擎检索 + 引用溯源.

检索路径: pgvector 预过滤向量检索 + PG BM25 -> RRF 融合, 一次查询跨
workspace 两层 [model, _domain_{type}]。未注册型号 fail-closed。

**为什么是自研的两路而不是单一向量检索**
----------------------------------------
中文规格书的精确标识符(``SR-1203`` / ``-54V`` / ``11.1A``)靠向量命不中,
必须留一路 BM25 兜底; 而要跨两个 workspace 做联合检索与元数据硬过滤, 又
不能用只服务单 workspace 的现成引擎(实测 LightRAG 1.5.7 的 mix 模式是
entities VDB + relationships VDB + chunks VDB 三次**向量**检索做 round-robin
合并, 不含 BM25, 且一个进程只服务一个 workspace)。故 chunk 层的 pgvector +
BM25 + RRF 自己留着 —— 细节见 ``retrieval/hybrid.py`` 的模块 docstring。

图谱导航曾由 LightRAG mix 承担, 现已随该依赖一并移除; 需要实体关系维度时
由 ``aterag.kg.entities`` 的抽取结果进 Semantica 图谱承担。
"""

from __future__ import annotations

from dataclasses import dataclass

from aterag.config import Settings
from aterag.ingest import pipeline
from aterag.models import EmbeddingClient
from aterag.registry import Registry
from aterag.retrieval import hybrid

# 命中来源 workspace 未在注册表两层装配内 -> 显式标记, 不得混入 model/domain。
# 标成 "model" 会让未注册 workspace 的命中被当成型号事实, 污染溯源分层与隔离判断。
UNREGISTERED_LAYER = "unregistered"


@dataclass
class SearchResult:
    content: str
    score: float
    layer: str  # model | domain | unregistered
    workspace_id: str
    section_path: str
    heading: str
    req_id: str
    priority: str
    rail: str
    source: str  # vector | bm25 | rrf | graph

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class RagService:
    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        embed: EmbeddingClient,
    ):
        self.settings = settings
        self.registry = registry
        self.embed = embed

    # ---------- workspace 装配 ----------
    def resolve(self, query: str, model_id: str | None):
        return self.registry.resolve_query(query, model_id)

    def workspaces(self, model_id: str) -> list[tuple[str, str]]:
        """[(workspace, layer), ...] 两层装配。"""
        entry = self.registry.products.get(model_id)
        layers = [(self.registry.model_workspace(model_id), "model")]
        if entry:
            layers.append((self.registry.domain_workspace(entry.domain), "domain"))
        return layers

    # ---------- 检索 ----------
    async def search(
        self,
        query: str,
        model_id: str | None = None,
        section_path: str | None = None,
        category: str | None = None,
        priority: str | None = None,
        top_k: int = 8,
    ) -> dict:
        resolved = self.resolve(query, model_id)
        ws_layers = self.workspaces(resolved.model_id)
        workspaces = [w for w, _ in ws_layers]
        layer_map = {w: layer for w, layer in ws_layers}

        vector_hits = await hybrid.vector_search(
            self.settings.postgres_dsn,
            self.embed,
            workspaces,
            query,
            top_k=top_k * 2,
            section_path=section_path,
            category=category,
            priority=priority,
        )
        # 未知 workspace 标 "unregistered" 而非 "model": 混入未注册 workspace 的命中
        # 会被误当成型号事实, 污染溯源分层与隔离判断
        hybrid.tag_layers(vector_hits, layer_map)

        bm25_hits = pipeline.bm25_search(
            self.settings.postgres_dsn,
            workspaces,
            query,
            top_k=top_k * 2,
            section_path=section_path,
            category=category,
            priority=priority,
        )
        hybrid.tag_layers(bm25_hits, layer_map)

        fused = pipeline.rrf_fuse(vector_hits, bm25_hits, top_k=top_k)
        results = [
            SearchResult(
                content=h.get("content", ""),
                score=round(h.get("rrf_score", h.get("score", 0.0)), 6),
                layer=h.get("layer", UNREGISTERED_LAYER),
                workspace_id=h.get("workspace_id", ""),
                section_path=h.get("section_path", ""),
                heading=h.get("heading", ""),
                req_id=h.get("req_id", ""),
                priority=h.get("priority", ""),
                rail=h.get("rail", ""),
                source="rrf",
            ).to_dict()
            for h in fused
        ]

        return {
            "model_id": resolved.model_id,
            "domain": resolved.domain,
            "model_id_source": resolved.source,
            "query": query,
            "filters": {
                "section_path": section_path,
                "category": category,
                "priority": priority,
            },
            "results": results,
        }

    # ---------- 实体查询 (PG 直查, 供 query_parameters 精确取值) ----------
    def query_entities(
        self,
        model_id: str,
        etype: str | None = None,
        keyword: str | None = None,
        section_path: str | None = None,
    ) -> list[dict]:
        import psycopg

        sql = "SELECT etype, eid, props FROM aterag_entities WHERE model_id = %s"
        params: list = [model_id]
        if etype:
            sql += " AND etype = %s"
            params.append(etype)
        if keyword:
            sql += " AND (eid ILIKE %s OR props::text ILIKE %s)"
            params += [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY eid"
        with psycopg.connect(self.settings.postgres_dsn) as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for etype_, eid, props in rows:
            # 章节过滤: 精确匹配或前缀匹配 (支持 4.3.2 命中 4.3.2.1)
            if (
                section_path
                and props.get("section_path", "") != section_path
                and not str(props.get("section_path", "")).startswith(section_path)
            ):
                continue
            out.append({"etype": etype_, "eid": eid, **props})
        return out


def _get_llm(settings: Settings):
    from aterag.models import LLMClient

    return LLMClient(settings)
