"""建库: 扩展安装、L0 共享 schema、型号 schema。

依据 V6.0:
    §5.8   RLS + 双时态叠加
    §5.9   三引擎的 schema 路由注入点
    §6.2   l0_term 放在 L0 共享 schema, 所有型号共用, 绝不复制
    §18.1.3 storage/schema.py —— L0 建库与 schema-per-型号

三条纪律 (§5.8.3 末尾):
    1. app.current_model 必须由认证中间件在连接建立时 SET, 禁止 SQL 层自行解析 token
    2. 每个新 schema/新表迁移必须带 RLS 策略并在 CI 加检查 —— 无 RLS 的表不允许上线
    3. 溯源导出必须走同一 view

纪律 1 的实现后果: 本模块提供 set_current_model() 但它**只**接受
已认证层传进来的值, 不接受 token。越权防护靠 RLS 兜底 (§5.8 说 RLS 是
「纵深防御再兜一层」, 而 schema 物理隔离是第一层)。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

# 型号键 -> schema 名。schema 名会进 SQL 标识符位置, 因此必须严格白名单校验,
# 不能靠参数化 (PostgreSQL 不支持 schema 名占位符)。
#
# 允许连字符: 真实型号就是 PA601-D54A / PN1000-48A / PN2000-24A 这种形式
# (data/registry.yaml)。型号键本身不进标识符位置 —— 进的是 schema 名,
# 而 schema 名由 model_schema_name 折叠连字符。两个位置的字符集不同,
# 所以是两个函数两个白名单。
_MODEL_KEY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,30}[A-Za-z0-9])?$")
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

L0_SCHEMA = "l0_term"

#: 每次建库都必须存在的扩展。缺任何一个都直接失败, 不降级。
#:
#: - vector       : pgvector, 全部向量列
#: - age          : Apache AGE, 图 (L2 / retrieval.graph)
#: - pg_textsearch: BM25 倒排 + 中文分词 (bm25 检索的唯一来源, 见 pyproject
#:                  里 bm25s 被移除的注释)
#: - zhparser     : 中文分词器本体, 供 public.chinese 文本搜索配置使用
REQUIRED_EXTENSIONS: tuple[str, ...] = ("vector", "age", "pg_textsearch", "zhparser")

#: 可选扩展。缺失只记录不失败 —— TimescaleDB 装在 timescaledb 镜像里,
#: 而板卡部署 (deploy/native/) 与容器部署 (deploy/postgres/Dockerfile)
#: 的可用扩展集不同, 硬要求会把容器路径也拖死。
#:
#: 注意扩展名是 ``timescaledb``, **不是** ``timescale``。后者是产品名与
#: schema 名, 不是扩展名 —— `CREATE EXTENSION timescale` 报
#: "extension timescale is not available"。板卡 192.168.5.25 实测:
#: `SELECT name FROM pg_available_extensions WHERE name LIKE 'timescale%'`
#: 只返回 `timescaledb`。与 deploy/native/04-init-postgres.sh:45 当年把
#: `vector` 写成 `pgvector` 是同一类错误 (名字对了, 扩展没那个名字)。
OPTIONAL_EXTENSIONS: tuple[str, ...] = ("timescaledb",)


class SchemaError(RuntimeError):
    """建库失败。命名不带具体类型, 因为调用方只能 fail-closed。"""


@dataclass(frozen=True)
class ExtensionStatus:
    """单个扩展的实测状态。

    用 ``SELECT extname FROM pg_extension`` 而非 ``pg_available_extensions``:
    前者回答「装没装」(可用性是 pg_available_extensions 的事),
    后者回答「装得了吗」—— 而 V6.0 §5.9 的教训正是只看后者会得到
    「向量库可用」的假结论。
    """

    name: str
    installed: bool
    version: str | None
    required: bool


def validate_model_key(model_key: str) -> str:
    """校验型号键, 返回规范化结果 (小写)。

    型号键会以两种方式进 DDL:
      1. 折叠后作为 schema 名 (标识符位置) —— 见 model_schema_name
      2. 原样作为字符串字面量 (DEFAULT 值、注释)

    所以这里必须是白名单而不是黑名单。长度上限 32 是为了 schema 名
    (PostgreSQL 上限 63) 加上前缀 ``pw_`` 后仍有余量给未来的表名后缀。
    """
    if not _MODEL_KEY_RE.match(model_key):
        raise SchemaError(
            f"型号键不合法: {model_key!r}。只允许字母、数字、连字符, "
            "长度 1~32, 且必须以字母或数字开头。型号键会进 schema 名与 RLS 策略, "
            "不能放宽字符集。"
        )
    return model_key.lower()


def model_schema_name(model_key: str) -> str:
    """``PA601-D54A`` -> ``pw_pa601_d54a``。

    连字符折叠为 ``_``。折叠后若仍不满足标识符白名单则拒绝 —— 双保险,
    避免将来放宽 _MODEL_KEY_RE 时静默产出非法 schema 名。
    """
    key = validate_model_key(model_key)
    folded = key.replace("-", "_")
    if not _SCHEMA_RE.match(f"pw_{folded}"):
        raise SchemaError(f"型号键折叠后不是合法 schema 名: {model_key!r} -> pw_{folded}")
    return f"pw_{folded}"


def quote_ident(name: str) -> str:
    """把标识符安全地加引号。

    只用白名单校验 + 双引号, 不做引号转义内插。理由: 一个能被
    ``_SCHEMA_RE`` 放行又被手工加了引号的标识符, 说明上游已经在
    拼字符串了, 那时再「安全地」转义只会把 bug 藏起来。
    """
    if not _SCHEMA_RE.match(name):
        raise SchemaError(f"标识符不合法: {name!r}")
    return f'"{name}"'


def quote_literal(value: str) -> str:
    """把字符串安全地转义成 PostgreSQL 字符串字面量。

    这是必要的: 型号键会出现在 DDL 的注释与 DEFAULT 值里, 而
    ``quote_ident`` 的白名单拦不住 ``'``。

    也不接受 NUL。PostgreSQL 的 text 类型无法存 U+0000, 传进去会报
    ``invalid byte sequence``。上游若真传进来, 应该在这里就看到明确的错,
    而不是等到 PostgreSQL 抛一个看起来无关的编码错误。
    """
    if "\x00" in value:
        raise SchemaError("标识符与字面量均不接受 NUL (U+0000): PostgreSQL text 无法存储。")
    return "'" + value.replace("'", "''") + "'"


def create_schema_sql(schema: str) -> str:
    """``CREATE SCHEMA IF NOT EXISTS``。

    幂等。型号 schema 的创建必须幂等, 因为 §18.6 的装载顺序里
    「型号 schema DDL」这一步会被反复跑 (每加一个型号跑一次)。
    """
    return f"CREATE SCHEMA IF NOT EXISTS {quote_ident(schema)}"


def create_l0_schema_sql() -> str:
    """建 L0 共享 schema。§6.2: 所有型号共用, 绝不复制。"""
    return create_schema_sql(L0_SCHEMA)


class CursorLike(Protocol):
    """本层需要的 cursor 能力子集。

    显式声明而非依赖 ``psycopg.Cursor``, 这样单元测试能用假 cursor 覆盖
    全部控制流而不需要真库, 且本模块在没有数据库驱动的环境 (纯静态检查)
    下仍可 import。

    契约测试 tests/contract/test_cursor_protocol.py 负责验证真 psycopg
    cursor 在运行时满足本协议 —— 协议只声明形状, 不保证实现符合。
    """

    def execute(self, query: str, params: Sequence[Any] | None = ...) -> Any: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...


def check_extensions(cur: CursorLike) -> list[ExtensionStatus]:
    """实测扩展安装状态。

    ``cur`` 是 psycopg 的 cursor。放在函数里而不是 import psycopg, 是为了让
    本模块在没有数据库驱动的环境 (纯静态检查 / 单元测试) 下仍可 import。
    """
    cur.execute(
        """
        SELECT e.extname, e.extversion
          FROM pg_extension e
        """
    )
    installed = {row[0]: row[1] for row in cur.fetchall()}

    statuses: list[ExtensionStatus] = []
    for name in REQUIRED_EXTENSIONS:
        statuses.append(
            ExtensionStatus(
                name=name,
                installed=name in installed,
                version=installed.get(name),
                required=True,
            )
        )
    for name in OPTIONAL_EXTENSIONS:
        statuses.append(
            ExtensionStatus(
                name=name,
                installed=name in installed,
                version=installed.get(name),
                required=False,
            )
        )
    return statuses


def missing_required(statuses: list[ExtensionStatus]) -> list[str]:
    """返回缺失的必需扩展名。空列表即通过。"""
    return [s.name for s in statuses if s.required and not s.installed]


def assert_extensions_installed(cur: CursorLike) -> list[ExtensionStatus]:
    """实测并在缺失时 fail-closed。

    返回实测状态供调用方记日志 (可选扩展的缺失也在这里可见)。

    为什么不降级: §3.3.1 把 pgvector + pg_textsearch + zhparser + AGE
    列为底座, 而 §5.9 明说「这一步不做, 隔离就是空的」。缺扩展时继续跑
    只会产出一批「看起来合理但检索不到」的结果, 比启动失败更难排查。
    deploy/native/04-init-postgres.sh:45 历史上写成
    ``CREATE EXTENSION IF NOT EXISTS pgvector`` —— 扩展真名是 ``vector``,
    该语句在 ON_ERROR_STOP=1 下会中断脚本。这类错误正是本函数要拦的。
    """
    statuses = check_extensions(cur)
    missing = missing_required(statuses)
    if missing:
        raise SchemaError(
            "缺少必需扩展: "
            + ", ".join(missing)
            + "。参考 deploy/postgres/initdb/01-extensions.sql。"
            " 注意扩展名是 vector 不是 pgvector (后者不存在)。"
        )
    return statuses
