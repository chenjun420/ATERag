"""Embedding 客户端 - 供应商通用适配层.

支持协议:
  openai      POST {base}/embeddings  {"model", "input"} -> data[].embedding
  dashscope   POST {base} (base 即完整端点)
              {"model", "input": {"texts": [...]}} -> output.embeddings[].embedding
协议自动识别 (URL 特征), 可用 EMBED_PROTOCOL 显式覆盖。
维度不假设: 启动时用探针文本实测 (dim_cache), 换供应商/模型零代码改动。
"""

from __future__ import annotations

import asyncio
from typing import Literal, Protocol

import httpx

from aterag.config import Settings

EmbedProtocol = Literal["openai", "dashscope"]


class _EmbedBackend(Protocol):
    async def embed(self, client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]: ...


class _OpenAIBackend:
    def __init__(self, base: str, model: str, api_key: str):
        self._url = base.rstrip("/") + "/embeddings"
        self._model = model
        self._api_key = api_key

    async def embed(self, client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
        resp = await client.post(
            self._url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self._model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # openai 协议保证与 input 顺序一致
        return [d["embedding"] for d in data]


class _DashScopeBackend:
    def __init__(self, base: str, model: str, api_key: str):
        self._url = (
            base if base.endswith("/embeddings") or "/text-embedding" in base else base.rstrip("/")
        )
        self._model = model
        self._api_key = api_key

    async def embed(self, client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
        resp = await client.post(
            self._url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self._model, "input": {"texts": texts}},
        )
        resp.raise_for_status()
        data = resp.json()["output"]["embeddings"]
        # 按 text_index 恢复原始顺序
        out: list[list[float] | None] = [None] * len(data)
        for item in data:
            out[item["text_index"]] = item["embedding"]
        if any(v is None for v in out):
            raise ValueError("dashscope embedding: incomplete response ordering")
        return [v for v in out if v is not None]


def detect_protocol(base_url: str) -> EmbedProtocol:
    if "/api/v1/services/embeddings" in base_url:
        return "dashscope"
    return "openai"  # 兼容模式端点 (含 /compatible-mode/v1 及一般 /v1)


class EmbeddingClient:
    """批量向量化的统一入口。"""

    def __init__(self, settings: Settings, timeout: float = 60.0):
        protocol: EmbedProtocol
        if settings.embed_protocol == "openai":
            protocol = "openai"
        elif settings.embed_protocol == "dashscope":
            protocol = "dashscope"
        else:
            protocol = detect_protocol(settings.embed_base)

        self.protocol = protocol
        self._batch_size = max(1, settings.embed_batch_size)
        self._model = settings.embed_model
        self._client = httpx.AsyncClient(timeout=timeout)
        if protocol == "openai":
            self._backend: _EmbedBackend = _OpenAIBackend(
                settings.embed_base, self._model, settings.embed_api_key
            )
        else:
            self._backend = _DashScopeBackend(
                settings.embed_base, self._model, settings.embed_api_key
            )
        self._dim_cache: int = settings.embed_dim or 0

    @property
    def dimension(self) -> int:
        return self._dim_cache

    async def aclose(self) -> None:
        await self._client.aclose()

    async def probe_dimension(self) -> int:
        """探针实测向量维度 (启动自检时调用)。"""
        vecs = await self.embed(["aterag-dimension-probe"])
        self._dim_cache = len(vecs[0])
        return self._dim_cache

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """按供应商上限分批 + 429/5xx 指数退避重试。"""
        results: list[list[float] | None] = [None] * len(texts)
        batches = [
            list(range(i, min(i + self._batch_size, len(texts))))
            for i in range(0, len(texts), self._batch_size)
        ]
        for batch in batches:
            vecs = await self._embed_batch_with_retry([texts[i] for i in batch])
            for idx, vec in zip(batch, vecs):
                results[idx] = vec
            if not self._dim_cache:
                self._dim_cache = len(vecs[0])
        if any(v is None for v in results):
            raise ValueError("embedding: missing results")
        return [v for v in results if v is not None]

    async def _embed_batch_with_retry(
        self, texts: list[str], retries: int = 3
    ) -> list[list[float]]:
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return await self._backend.embed(self._client, texts)
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                last_err = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                transient = status is None or status == 429 or status >= 500
                if attempt < retries and transient:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                break
        raise RuntimeError(f"embedding batch failed after {retries + 1} attempts: {last_err}")
