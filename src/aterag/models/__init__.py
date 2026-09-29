"""模型适配层: LLM (OpenAI 兼容) + Embedding (通用协议适配)."""
from aterag.models.embed_client import EmbeddingClient
from aterag.models.llm_client import LLMClient

__all__ = ["EmbeddingClient", "LLMClient"]
