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
    # 人工注记 (兜底判据的签字记录) 属于**运行时数据**, 不属于系统。
    #
    # 它逐条对应某个客户型号的规格书条款, 进版本库等于把客户判据连同需求编号
    # 一起公开; 而仓库应当只含系统 (代码 + 认知词表 + 通用规则) 与使用说明。
    # 部署后由使用者在自己的环境里用 scripts/review_annotation.py 生成并签字,
    # 用法见 docs/使用说明.md。
    #
    # 路径相对 CWD 解析, 默认落在仓库外的 data/ 下。缺失不报错: 注记是兜底,
    # 没有它只是那部分需求切不出条件, 不该让整个抽取失败
    # (AnnotationBook.load 对不存在的路径返回空书)。
    annotations_dir: str = "data/annotations"
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
