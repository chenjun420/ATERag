"""集中配置 (pydantic-settings, 全部来自 .env / 环境变量)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- LLM ----
    llm_base: str
    llm_model: str
    llm_api_key: str = ""

    # ---- Embedding ----
    embed_base: str
    embed_model: str
    embed_api_key: str = ""
    embed_protocol: str = "auto"  # auto | openai | dashscope
    embed_batch_size: int = 10
    embed_dim: int = 0  # 0 = 启动时探针实测

    # ---- 存储 (远程唯一后端) ----
    postgres_dsn: str
    qdrant_url: str
    qdrant_collection: str = "lightrag_vectors"

    # ---- 知识分层 ----
    registry_path: str = "registry.yaml"
    domain_rules_dir: str = "domain_rules"
    # 表结构档案: 表头语义 (编号/项目/遥测量/信号要求...) 的唯一来源。
    # 相对 CWD 解析, 缺失直接报错 —— 静默丢列比报错更危险。
    table_schemas_path: str = "config/table_schemas.yaml"
    # 抽取侧档案: 章节选择/剔除词/角色先验 + 条件规则库 + 人工注记目录
    doc_profiles_path: str = "config/doc_profiles.yaml"
    condition_patterns_path: str = "config/condition_patterns.yaml"
    annotations_dir: str = "config/annotations"
    common_workspace: str = "_common"
    domain_workspace_prefix: str = "_domain_"

    # ---- MCP ----
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8080

    # ---- Semantica 语义图 (双写目标) ----
    semantica_enabled: bool = False
    semantica_graph: str = "power_rules"

    @property
    def llm_api_key_masked(self) -> str:
        return (self.llm_api_key[:6] + "...") if self.llm_api_key else "(empty)"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # 由环境变量/.env 提供
