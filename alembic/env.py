"""Alembic 环境。

连接串来源: ``aterag.config.Settings.postgres_dsn``, 与应用运行时同一份。
不读 alembic.ini 的 ``sqlalchemy.url`` —— 那会让两处配置漂移。

离线模式 (--sql) 需要显式给 DSN:
    alembic upgrade head --sql > schema.sql
离线模式下没有 Settings 可读 (可能有, 但命令行意图是产出可审计的
SQL 文本, 而 Settings 可能指向开发库), 所以离线一律要求 ``-x dsn=``。

型号 schema 的处理
-----------------
迁移只负责**基线** (L0 + 公共结构)。``pw_<model_key>`` schema 由
``storage.schema.create_model_schema()`` 在部署时按型号创建, 不进版本库。

理由: 型号是部署期事实 (data/registry.yaml), 不是代码。若每次新增型号
都要写一个 migration, 则「加一个型号」与「改代码」被绑死, 而前者本该
只是一条登记。方案 §18.6 的装载顺序也把「型号 schema DDL」列为装载步骤,
而不是迁移步骤。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 `import aterag` 在未安装包的源码检出下也能工作
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aterag.config import Settings  # noqa: E402  (必须在 sys.path 调整之后)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None
"""刻意为 None。

本项目不用 SQLAlchemy ORM (既有实现是裸 psycopg), 因此 autogenerate
无 metadata 可依据。若设成 None, ``alembic revision --autogenerate``
不会报错但会生成空迁移 —— 那比报错更危险, 因为它看起来「成功」了。

所以本项目不使用 --autogenerate。迁移一律手写, 理由是 DDL 里含
partial unique index、RLS 策略、CHECK 约束与触发器, 这些 autogenerate
要么生成不出来, 要么生成出来语义与手写不同 (典型是 RLS 策略被丢掉)。
"""


def _url() -> str:
    """取连接串。"""
    if context.is_offline_mode():
        offline_url = config.get_main_option("sqlalchemy.url", "")
        x_args = context.get_x_argument(as_dictionary=True)
        dsn = x_args.get("dsn") or offline_url
        if not dsn:
            raise SystemExit(
                "离线模式必须给 DSN: alembic upgrade head --sql -x dsn=postgresql://...\n"
                " 离线模式不读 Settings, 因为命令行的意图是产出可审计的 SQL 文本, "
                "而 Settings 可能指向开发库。"
            )
        return dsn
    return Settings().postgres_dsn


def run_migrations_offline() -> None:
    """产出 SQL 文本, 不连库。"""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # 迁移里含 RLS 策略与 partial index, 都必须原样出现在 SQL 文本里。
        # 默认的 batch 模式会把 CREATE TABLE 拆成临时表 + ALTER, 那会让
        # 审计者读到一份与实际执行结构不同的文本。
        transactional_ddl=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """连库执行。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transactional_ddl=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
