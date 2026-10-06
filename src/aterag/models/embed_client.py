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
    def __init__(self, base: str, model: str, api_key: str, dim: int = 0):
        self._url = base.rstrip("/") + "/embeddings"
        self._model = model
        self._api_key = api_key
        self._dim = dim

    async def embed(self, client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {"model": self._model, "input": texts}
        if self._dim:
            # **必须显式下发 dimensions**。ADR-013 定死全系统1024 维(halfvec),
            # 而原生维度更高的模型会默认返回全维度 —— 实测 doubao-embedding-vision
            # 原生 2048 维, 不传就落成 2048, 与库里 `vector(1024)` / HNSW 索引
            # 对不上, 表现为「探测维度」与建表维度不一致的运行期错误。
            #
            # 探测到的维度不能自动回填: ADR-013 要求「探测失败不得回退默认值」,
            # 同理「探测成功但没记录」也不能靠默认值补上 —— 必须显式配置。
            payload["dimensions"] = self._dim
        resp = await client.post(
            self._url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # 按响应里的 ``index`` 排序, **不假设返回顺序等于输入顺序**。
        # 实测 ark 的响应确实带 ``index``, 而旧注释写「openai 协议保证与 input
        # 顺序一致」—— 协议没有这条保证, 实测也未必成立。顺序错掉不会报错,
        # 只会让每条 chunk 的向量对应到别人的文本, 而检索看起来完全正常。
        if any("index" in item for item in data):
            ordered = sorted(data, key=lambda d: d["index"])
            if [d["index"] for d in ordered] != list(range(len(ordered))):
                raise ValueError(
                    f"openai embedding: index 不连续, 得到 "
                    f"{[d['index'] for d in ordered]} (期望 0..{len(ordered) - 1})"
                )
            return [d["embedding"] for d in ordered]
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
        # 顺序键有两个名字: 实测 ``qwen3.7-text-embedding`` 返回 ``text_index``,
        # 而 ``qwen3.7-text-embedding-flash`` 只返回 ``index`` —— 同一供应商的
        # 两个模型响应形状不一致。写死 ``text_index`` 时 flash 版直接
        # ``KeyError: 'text_index'``, 而 KeyError 出现在 4 次重试的包装里,
        # 表现为「embedding batch failed」, 看不出是响应形状问题。
        #
        # 两个键都缺时**不能**回退成「按返回顺序用」: 顺序错掉不会报错, 只会让
        # 每条 chunk 的向量对应到别人的文本 —— 而检索看起来完全正常。
        out: list[list[float] | None] = [None] * len(data)
        for item in data:
            key = "text_index" if "text_index" in item else "index"
            if key not in item:
                raise ValueError(
                    f"dashscope embedding: 响应缺顺序键 (text_index/index), got {sorted(item)}"
                )
            out[item[key]] = item["embedding"]
        if any(v is None for v in out):
            raise ValueError(
                f"dashscope embedding: 响应不完整, 期望 {len(data)} 条, "
                f"实到 {sum(1 for v in out if v is not None)} 条"
            )
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
                settings.embed_base, self._model, settings.embed_api_key, settings.embed_dim
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
