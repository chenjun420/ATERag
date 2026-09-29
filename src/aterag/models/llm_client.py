"""LLM 客户端 - OpenAI 兼容 chat completions 协议.

供应商可替换: 只要提供 OpenAI 兼容 /chat/completions 即可。
模型名全部来自配置, 后期更换模型不改代码。
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from aterag.config import Settings


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings, timeout: float = 120.0):
        self._base = settings.llm_base.rstrip("/")
        self._model = settings.llm_model
        self._api_key = settings.llm_api_key
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise LLMError(f"transient HTTP {resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except (httpx.HTTPError, LLMError, KeyError, IndexError) as e:
                last_err = e
                if attempt < retries:
                    await asyncio.sleep(min(2**attempt, 8))
        raise LLMError(f"chat failed after {retries + 1} attempts: {last_err}")

    async def list_models(self) -> list[str]:
        resp = await self._client.get("/models")
        resp.raise_for_status()
        data = resp.json()
        return [m.get("id", "") for m in data.get("data", [])]
