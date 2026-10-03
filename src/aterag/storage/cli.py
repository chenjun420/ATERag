"""``aterag-db`` —— 存储底座的命令行入口。

V6.0 §18.1.3 要求 ``storage/`` 提供建库入口, ``pyproject.toml`` 的
``[project.scripts]`` 已登记 ``aterag-db = "aterag.storage.cli:main"``。
本模块提供四个子命令:

    aterag-db check                 实测扩展安装状态 (fail-closed)
    aterag-db upgrade               跑 alembic 建 L0 共享层
    aterag-db init MODEL [MODEL..]  建型号 schema (§18.5 第 2/3/4 分区)
    aterag-db verify MODEL          门禁断言: 无 RLS 的表不允许上线
    aterag-db sql MODEL             只打印 DDL, 不连库

分工
----
L0 共享层走 alembic (见 ``alembic/env.py`` 的「型号 schema 的处理」);
型号 schema 走本模块 —— 因为型号是部署期事实 (``data/registry.yaml``),
不是代码, 不该为每加一个型号写一个 migration。

失败语义
--------
全部 fail-closed。扩展缺失、RLS 缺失、型号键非法一律非零退出并打印
可操作的修复指引, 不降级、不猜测。理由见 ``storage/schema.py`` 的
:func:`assert_extensions_installed`。

设计约束
--------
``import psycopg`` 放在函数内 —— 本模块在无数据库驱动的环境 (纯静态检查、
CI 的 lint 阶段) 下必须仍可 import, 与 ``storage.schema`` /
``storage.rls`` 同一纪律。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from .model_schema import (
    MODEL_TABLES,
    PUBLIC_TABLES,
    TIMESCALE_EXTENSION,
    hypertable_available_sql,
    model_ddl,
    model_schema_ddl,
    public_ddl,
)
from .rls import (
    assert_no_unprotected_tables,
    assert_schema_built,
    rls_ddl,
    schema_tables_sql,
)
from .schema import (
    assert_extensions_installed,
    l0_schema_exists_sql,
    model_schema_name,
)

__all__ = ["main"]


class _Conn(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


@contextmanager
def _connect(dsn: str | None) -> Iterator[_Conn]:
    """按 DSN 连库, 退出时必定关闭。

    DSN 优先取 ``--dsn``; 缺省回落到 ``aterag.config.Settings.postgres_dsn``
    —— 与应用运行时、alembic 同一份配置, 不在这里另开一个 env 变量名。
    """
    import psycopg  # 局部 import, 见模块 docstring

    resolved = dsn
    if resolved is None:
        from ..config import Settings

        resolved = Settings().postgres_dsn  # type: ignore[call-arg]

    conn = psycopg.connect(resolved)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _run(cur: Any, sql: str) -> None:
    """执行一条语句并把结果集收干。

    收干是必要的: psycopg 下同连接连续 execute 时, 未取完的结果集会
    触发「another command is already in progress」。
    """
    cur.execute(sql)
    try:
        cur.fetchall()
    except Exception:  # noqa: BLE001 - 非 SELECT 语句本就无结果集
        pass


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------


def cmd_check(conn: _Conn, args: argparse.Namespace) -> int:
    """实测扩展安装状态。

    §18.5 第 0 分区与 ``storage/schema.py`` 的 REQUIRED/OPTIONAL_EXTENSIONS
    对齐。全部扩展由 :func:`check_extensions` 一处枚举并打印 —— 曾经额外
    单独打一行 timescaledb, 而 timescaledb 已在 OPTIONAL_EXTENSIONS 里, 于是
    板卡上同一行连打两次。

    缺 timescaledb 时补一句说明, 因为它不是「装了没用上」而是
    「hypertable 建不出来」, 且装包后还要 shared_preload_libraries + 重启。
    """
    from .schema import check_extensions, missing_required

    with conn.cursor() as cur:
        statuses = check_extensions(cur)

    for st in statuses:
        mark = "OK " if st.installed else ("MISS" if st.required else "opt ")
        print(f"[{mark}] {st.name:<16} {st.version or '(未安装)'}")

    ts = next((s for s in statuses if s.name == TIMESCALE_EXTENSION), None)
    if ts is not None and not ts.installed:
        print(
            f"\n注: 缺 {TIMESCALE_EXTENSION} —— hypertable 段将跳过。"
            f"\n    扩展名是 {TIMESCALE_EXTENSION} (不是 timescale)。"
            "\n    且它要求 shared_preload_libraries 含该库并重启 PostgreSQL,"
            f"\n    否则 CREATE EXTENSION 报 'must be preloaded'。"
            "\n    不需要自动分区的部署用 init --no-hypertable。"
        )

    missing = missing_required(statuses)
    if missing:
        print(
            "\n缺少必需扩展: " + ", ".join(missing) + "\n"
            "参考 deploy/postgres/initdb/01-extensions.sql。"
            "注意扩展名是 vector 不是 pgvector (后者不存在)。",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_upgrade(conn: _Conn, args: argparse.Namespace) -> int:
    """跑 alembic 建 L0 共享层。

    不自己拼 ``alembic_version`` —— 版本记录归 alembic, 否则两条路径会
    互相看不见对方的迁移。

    DSN 用**原样**的入参, 不从连接对象反查: ``psycopg.Connection.info.dsn``
    返回的是 libpq 的 keyword/value 形式 (``make_conninfo()`` 的输出, 如
    ``dbname=power_specs host=127.0.0.1 ...``), 而 SQLAlchemy 的
    ``make_url`` 只认 URL 形式。板卡上就是这一步报的
    ``ArgumentError: Could not parse SQLAlchemy URL`` —— 与迁移内容无关,
    纯粹是 DSN 形态不匹配。
    """
    from alembic import command
    from alembic.config import Config

    ini = _alembic_ini()
    cfg = Config(str(ini))
    dsn = args.dsn if args.dsn else _settings_dsn()
    # set_main_option 走 ConfigParser 插值: DSN 里若含裸 % (密码里很常见)
    # 会被当成插值语法。转义成 %%。
    cfg.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    command.upgrade(cfg, "head")
    print("L0 迁移已升级到 head。")
    return 0


def _settings_dsn() -> str:
    """从应用配置取 DSN。惰性导入 —— 见 alembic/env.py 同名函数的说明。"""
    from ..config import Settings

    return Settings().postgres_dsn  # type: ignore[call-arg]


def cmd_init(conn: _Conn, args: argparse.Namespace) -> int:
    """建型号 schema (§18.5 第 2/3/4 分区)。

    顺序: public 附件表 -> 表 -> hypertable -> RLS。

    public 必须最先: ``doc.object_id`` 是指向 ``public.document_object``
    的外键, 被引表不存在时建表直接失败。

    hypertable 必须在 RLS 之前 ——
    TimescaleDB 把 hypertable 拆成子表, FORCE ROW LEVEL SECURITY 要作用
    在父表上才覆盖全部 chunk; 反过来则 chunk 裸奔。

    ``--no-hypertable`` 供无 TimescaleDB 的部署跳过第 3 分区 (此时
    5 张时序表退化为普通表, 仍可用, 只是失去自动分区)。

    三项前置检查 (板卡首次部署时全部踩到, 且报错都指向错误的层):
        1. 必需扩展 —— 缺 ``vector`` 时建表到一半才炸, 且错误是
           ``type "vector" does not exist`` 而非「扩展没装」。
        2. L0 schema —— 型号表的 concept_id 外键全指向 l0_term.concept,
           L0 未建时错误是 ``schema "l0_term" does not exist``, 不指向
           「先跑 upgrade」。
        3. TimescaleDB —— 未装时报 ``extension "timescaledb" must be
           preloaded``, 而真因是 shared_preload_libraries 里没有它。

    前置检查的价值在于把「部署顺序错了」与「代码有 bug」区分开: 三者都
    曾表现为建到一半的底层错误, 读起来像 DDL 写错了。
    """
    models: list[str] = list(args.models)
    created: list[str] = []

    with conn.cursor() as cur:
        try:
            assert_extensions_installed(cur)
        except Exception as exc:  # noqa: BLE001 - SchemaError 在此转成退出码
            print(f"[FAIL] 前置检查: {exc}", file=sys.stderr)
            return 1

        cur.execute(l0_schema_exists_sql())
        if cur.fetchone() is None:
            print(
                "[FAIL] 前置检查: L0 共享 schema 尚未建好。\n"
                "       型号表的 concept_id 外键全部指向 l0_term.concept。\n"
                "       请先跑: aterag-db upgrade",
                file=sys.stderr,
            )
            return 1

        if not args.no_hypertable:
            cur.execute(hypertable_available_sql())
            if cur.fetchone() is None:
                print(
                    "[FAIL] 前置检查: 未装 timescaledb 扩展, 而本次要建 hypertable。\n"
                    "       扩展名是 timescaledb (不是 timescale —— 后者是产品名)。\n"
                    "       另外它要求 shared_preload_libraries 含 timescaledb\n"
                    "       并重启 PostgreSQL, 光 CREATE EXTENSION 会报\n"
                    "       'extension \"timescaledb\" must be preloaded'。\n"
                    "       若该部署不需要自动分区, 加 --no-hypertable。",
                    file=sys.stderr,
                )
                return 1

        for sql in public_ddl():
            _run(cur, sql)
    print(f"已建 public 附件表: {', '.join(PUBLIC_TABLES)}")

    for model_key in models:
        schema = model_schema_name(model_key)
        if args.no_hypertable:
            stmts = model_ddl(model_key) + rls_ddl(model_key)
        else:
            stmts = model_schema_ddl(model_key)
        with conn.cursor() as cur:
            for sql in stmts:
                _run(cur, sql)
        created.append(schema)
        print(f"已建 {schema}: {len(stmts)} 条语句")

    print("\n已建型号 schema: " + ", ".join(created))
    return 0


def cmd_verify(conn: _Conn, args: argparse.Namespace) -> int:
    """门禁断言: 目标 schema 已建成, 且无未受 RLS 保护的表。

    §5.8.3 纪律 2 + §5.10.5 断言 5 + §18.4.2 W0 验收判据①。

    **先查表数再查 RLS。** ``assert_no_unprotected_tables`` 对空 schema 恒真
    (没有表就没有未保护的表), 所以「什么都没建」会被报成「检查通过」。
    板卡首次部署时正是如此: ``init`` 因缺 l0_term 整体回滚, 留下两个空
    schema, 门禁却打印 [OK] —— 判据为空集时门禁无效, 同 §18.10 注 5 的
    「有生成器但无门禁」是同一类问题。
    """
    rc = 0
    for model_key in args.models:
        schema = model_schema_name(model_key)
        with conn.cursor() as cur:
            try:
                assert_schema_built(cur, schema, expected=len(MODEL_TABLES))
                assert_no_unprotected_tables(cur, schema)
                cur.execute(schema_tables_sql(schema))
                n_tables = len(cur.fetchall())
            except Exception as exc:  # noqa: BLE001 - RlsError 在此转成退出码
                print(f"[FAIL] {schema}\n       {exc}", file=sys.stderr)
                rc = 1
                continue
        print(
            f"[OK  ] {schema}: {n_tables} 张表全部已 ENABLE + FORCE ROW LEVEL SECURITY"
        )
    return rc


def cmd_sql(_conn: _Conn, args: argparse.Namespace) -> int:
    """只打印 DDL, 不连库。供审计与 diff 用。"""
    print(f"-- ==== {model_schema_name(args.models[0])} ====")
    for sql in model_schema_ddl(args.models[0]):
        print(sql.rstrip() + ";")
    return 0


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------

_SUBCOMMANDS = {
    "check": cmd_check,
    "upgrade": cmd_upgrade,
    "init": cmd_init,
    "verify": cmd_verify,
    "sql": cmd_sql,
}


def _alembic_ini() -> Any:
    """定位 alembic.ini。

    从本文件向上找仓库根 (含 ``pyproject.toml`` 的目录)。源码检出下必然
    找到; 作为 wheel 安装时找不到 —— 此时 alembic 迁移本就不随包分发,
    应报清晰的错而不是 ``FileNotFoundError``。
    """
    from pathlib import Path

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alembic.ini"
        if candidate.is_file() and (parent / "pyproject.toml").is_file():
            return candidate
    raise SystemExit(
        "找不到 alembic.ini (向上未找到同时含 alembic.ini 与 pyproject.toml 的目录)。\n"
        "  alembic 迁移只随源码检出分发, 不随 wheel 分发。若在已安装的包上执行,\n"
        "  请改为直接执行 seed/schema_full.sql。"
    )


def _dsn_of(conn: _Conn) -> str:
    """从已建立的连接反查 DSN。

    **不要用。** psycopg 的 ``Connection.info.dsn`` 返回 libpq 的
    keyword/value 形式 (``dbname=power_specs host=127.0.0.1 ...``, 由
    ``make_conninfo()`` 生成), 不是 URL; SQLAlchemy 的 ``make_url`` 只认
    URL 形式, 板卡上 ``aterag-db upgrade`` 就因此报
    ``ArgumentError: Could not parse SQLAlchemy URL``。
    cmd_upgrade 改用 :func:`_resolved_dsn`。本函数保留是为了让这个坑有据
    可查, 不作为调用路径。
    """
    info = getattr(conn, "info", None)
    dsn = getattr(info, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return dsn
    return _resolved_dsn(None)


def _resolved_dsn(args: argparse.Namespace | None) -> str:
    """取 DSN: ``--dsn`` 优先, 否则读应用配置。惰性导入 Settings。"""
    if args is not None and args.dsn:
        return str(args.dsn)
    from ..config import Settings

    return Settings().postgres_dsn  # type: ignore[call-arg]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aterag-db",
        description="ATERag 存储底座管理 (L0 迁移 / 型号 schema / RLS 门禁)",
    )
    p.add_argument("--dsn", default=None, help="PostgreSQL DSN, 缺省读 Settings.postgres_dsn")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="实测扩展安装状态 (fail-closed)").set_defaults(
        func=cmd_check
    )
    sub.add_parser("upgrade", help="跑 alembic 建 L0 共享层").set_defaults(
        func=cmd_upgrade
    )

    p_init = sub.add_parser("init", help="建型号 schema")
    p_init.add_argument("models", nargs="+", help="型号键, 如 PA601-D54A")
    p_init.add_argument(
        "--no-hypertable",
        action="store_true",
        help="跳过 TimescaleDB hypertable (无该扩展的部署用)",
    )
    p_init.set_defaults(func=cmd_init)

    p_verify = sub.add_parser("verify", help="门禁: 无 RLS 的表不允许上线")
    p_verify.add_argument("models", nargs="+", help="型号键")
    p_verify.set_defaults(func=cmd_verify)

    p_sql = sub.add_parser("sql", help="只打印 DDL, 不连库")
    p_sql.add_argument("models", nargs=1, help="型号键")
    p_sql.set_defaults(func=cmd_sql)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func = args.func
    # sql 子命令不连库; 其余需要连接。
    if args.command == "sql":
        return int(func(None, args))
    with _connect(args.dsn) as conn:
        return int(func(conn, args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
