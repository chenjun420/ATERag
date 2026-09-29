"""RAG 检索服务: 三层 workspace 装配 + 双引擎检索 + 引用溯源.

路由策略:
  带章节/类别/优先级过滤 -> Qdrant 预过滤向量检索 + PG BM25 -> RRF 融合
  无过滤                -> Qdrant+BM25 融合 + LightRAG mix (图导航) 补充
隔离: workspace 三层 [model, _domain_{type}, _common]; 未注册型号 fail-closed。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from qdrant_client import QdrantClient

from aterag.config import Settings
from aterag.ingest import pipeline
from aterag.models import EmbeddingClient
from aterag.registry import Registry

# 命中来源 workspace 未在注册表三层装配内 -> 显式标记, 不得混入 model/domain/common。
# 标成 "model" 会让未注册 workspace 的命中被当成型号事实, 污染溯源分层与隔离判断。
UNREGISTERED_LAYER = "unregistered"


@dataclass
class SearchResult:
    content: str
    score: float
    layer: str          # model | domain | common | unregistered
    workspace_id: str
    section_path: str
    heading: str
    req_id: str
    priority: str
    rail: str
    source: str         # vector | bm25 | rrf | graph

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
        self._qdrant = QdrantClient(url=settings.qdrant_url, timeout=60)
        self._lightrag_cache: dict[str, object] = {}

    # ---------- workspace 装配 ----------
    def resolve(self, query: str, model_id: str | None):
        return self.registry.resolve_query(query, model_id)

    def workspaces(self, model_id: str) -> list[tuple[str, str]]:
        """[(workspace, layer), ...] 三层装配。"""
        entry = self.registry.products.get(model_id)
        layers = [(self.registry.model_workspace(model_id), "model")]
        if entry:
            layers.append((self.registry.domain_workspace(entry.domain), "domain"))
        layers.append((self.registry.common_workspace, "common"))
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
        use_graph: bool = True,
    ) -> dict:
        resolved = self.resolve(query, model_id)
        ws_layers = self.workspaces(resolved.model_id)
        workspaces = [w for w, _ in ws_layers]
        layer_map = {w: layer for w, layer in ws_layers}

        vector_hits = await pipeline.qdrant_search(
            self._qdrant,
            self.embed,
            workspaces,
            query,
            top_k=top_k * 2,
            section_path=section_path,
            category=category,
            priority=priority,
        )
        for h in vector_hits:
            # 未知 workspace 标 "unregistered" 而非 "model": 混入未注册 workspace 的命中
            # 会被误当成型号事实, 污染溯源分层与隔离判断
            h["layer"] = layer_map.get(h.get("workspace_id", ""), UNREGISTERED_LAYER)

        bm25_hits = pipeline.bm25_search(
            self.settings.postgres_dsn,
            workspaces,
            query,
            top_k=top_k * 2,
            section_path=section_path,
            category=category,
            priority=priority,
        )
        for h in bm25_hits:
            h["layer"] = layer_map.get(h.get("workspace_id", ""), UNREGISTERED_LAYER)

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

        graph_results: list[dict] = []
        if use_graph and not section_path:
            graph_results = await self._graph_search(resolved.model_id, query, top_k)

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
            "graph_results": graph_results,
        }

    async def _graph_search(self, model_id: str, query: str, top_k: int) -> list[dict]:
        """LightRAG mix 模式 (图+向量), 仅型号 workspace。

        only_need_context=True 返回的是整段检索上下文 (实体 + 关系 + 文档块 + 引用表,
        实测可达 40K+ 字符), 必须按段落解析成独立引用块, 不能整段截断 —— 否则真正的
        命中块会落在截断线之外, 图检索退化为无信息片段。
        """
        try:
            from lightrag import QueryParam

            rag = self._get_lightrag(model_id)
            await rag.initialize_storages()  # 惰性初始化 (graph_name 依赖 workspace)
            result = await rag.aquery(
                query, param=QueryParam(mode="mix", top_k=top_k, only_need_context=True)
            )
            # only_need_context=True 返回检索上下文 (不调 LLM 生成), 解析为引用片段
            if isinstance(result, dict):
                chunks = result.get("chunks", {}).get("chunks", [])
                return [
                    {
                        "content": c.get("content", ""),
                        "source": "graph-mix",
                        "layer": "model",
                    }
                    for c in chunks[:top_k]
                    if c.get("content")
                ]
            return self._parse_graph_context(str(result), top_k)
        except Exception as e:  # noqa: BLE001  # 图检索失败不阻塞主检索
            return [{"content": f"(graph retrieval unavailable: {e})", "source": "graph-mix", "layer": "model"}]

    @staticmethod
    def _parse_graph_context(context: str, top_k: int) -> list[dict]:
        """把 LightRAG 检索上下文拆成可引用的独立块。

        上下文结构 (LightRAG mix, only_need_context):
            Knowledge Graph Data (Entity):      ```json [...]```
            Knowledge Graph Data (Relationship): ```json [...]```
            Document Chunks:                    ```json [{"reference_id","content"}, ...]```
            Reference Document List:            ```...```
        优先取 Document Chunks (真正的规格书原文), 其次取关系, 最后才退回截断的原文。
        """
        def _section(header_keyword: str) -> list[dict]:
            """取 header 关键字之后第一个 ```json 块并解析为 dict 列表。

            LightRAG 输出的是 NDJSON (每行一个 JSON 对象) 而非 JSON 数组, 因此先整体
            json.loads, 失败则退回 raw_decode 逐个读取连续 JSON 值。
            """
            idx = context.find(header_keyword)
            if idx < 0:
                return []
            fence = context.find("```json", idx)
            if fence < 0:
                return []
            end = context.find("```", fence + 7)
            if end < 0:
                end = len(context)
            raw = context[fence + 7 : end]

            def _as_list(data: object) -> list[dict]:
                if isinstance(data, list):
                    return [d for d in data if isinstance(d, dict)]
                return [data] if isinstance(data, dict) else []

            try:
                return _as_list(json.loads(raw))
            except json.JSONDecodeError:
                pass
            out: list[dict] = []
            pos = 0
            while pos < len(raw):
                nl = raw.find("\n", pos)
                if nl < 0:
                    break
                line = raw[pos:nl].strip()
                pos = nl + 1
                if not line:
                    continue
                try:
                    out.extend(_as_list(json.loads(line)))
                except json.JSONDecodeError:
                    continue  # 非 JSON 行 (表头/说明) 跳过
            return out

        out: list[dict] = [
            {"content": c.get("content", ""), "source": "graph-mix", "layer": "model"}
            for c in _section("Document Chunks")
            if c.get("content")
        ]
        if not out:
            out = [
                {
                    "content": f"{e.get('entity', '')} ({e.get('type', '')}): {e.get('description', '')}",
                    "source": "graph-mix-entity",
                    "layer": "model",
                }
                for e in _section("Knowledge Graph Data (Entity)")
                if e.get("entity")
            ]
        if not out:
            out = [
                {
                    "content": f"{e.get('source', '')} -[{e.get('keywords', '')}]-> {e.get('target', '')}",
                    "source": "graph-mix-relation",
                    "layer": "model",
                }
                for e in _section("Knowledge Graph Data (Relationship)")
            ]
        if not out:
            # 兜底: 仍要给引用内容, 但显式标注为未解析, 便于上层识别
            out = [{"content": context[:2000], "source": "graph-mix-raw", "layer": "model"}]
        return out[:top_k]

    def _get_lightrag(self, model_id: str):
        from aterag.ingest.pipeline import build_lightrag

        if model_id not in self._lightrag_cache:
            from aterag.ingest.pipeline import lrag_workspace

            self._lightrag_cache[model_id] = build_lightrag(
                self.settings,
                lrag_workspace(model_id),
                self.embed,
                _get_llm(self.settings),
                self.embed.dimension,
            )
        return self._lightrag_cache[model_id]

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
            if (section_path
                    and props.get("section_path", "") != section_path
                    and not str(props.get("section_path", "")).startswith(section_path)):
                continue
            out.append({"etype": etype_, "eid": eid, **props})
        return out


def _get_llm(settings: Settings):
    from aterag.models import LLMClient

    return LLMClient(settings)
