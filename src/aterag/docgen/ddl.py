"""全量 DDL 汇编 (§18.5)。产出 ``seed/schema_full.sql``。

§18.5 把 ``schema_full.sql`` 定义为「可直接 ``psql`` 执行」的交付物,
并规定了五个分区与各自的权威位置:

    0. 扩展
    1. L0 共享 (第六章 l0_term、§18.3.1 formula)
    2. 型号 schema (第三/五章 + 第十六/十七章 + 附录H)
    3. hypertable (TimescaleDB)
    4. RLS 策略 (§5.8)
    5. 触发器与函数 (第九章)

单一真相源
----------
本模块**不重新实现**任何 DDL, 只做三件编排的事:

* 第 1 分区直接取 ``alembic upgrade head --sql`` 的离线输出。L0 层的
  真相源是迁移版本库, 复制一份到 .sql 里必然漂移 —— 而漂移的表现是
  「迁移跑过了但表结构是旧的」, 比没有 .sql 更难排查。
* 第 2~4 分区取 :mod:`aterag.storage.model_schema`, 它是型号 schema 的
  真相源 (与 ``aterag-db init`` 走的是同一份生成器)。
* 第 5 分区的触发器分散在各表定义里 (``update_tsv`` / ``fact_as_of``),
  因此天然落在第 2 分区内, 不重复输出。

型号参数化
----------
§18.5 ③ 给了两条路: 替换占位符, 或输出为 psql 变量。这里选后者 ——
输出一份文件即可服务任意型号, 而不是每个型号一份。写法是
``:"model_key"`` (标识符位置) 与 ``:'model_key'`` (字面量位置),
两种引号形式**不能互换**, 理由见 :class:`aterag.storage.rls.SchemaRef`。

用法::

    psql -v ON_ERROR_STOP=1 -v model_key=pw_sr5400 \\
         -f seed/schema_full.sql -d power_rag_test
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from ..storage.model_schema import (
    hypertable_ddl_for_ref,
    model_ddl_for_ref,
    public_ddl,
    table_batches_for_ref,
)
from ..storage.rls import psql_schema_ref, rls_ddl_for_ref, schema_ref
from ..storage.schema import L0_SCHEMA

__all__ = [
    "DDL_SECTIONS",
    "MIN_POSTGRES_MAJOR",
    "PSQL_VAR",
    "SchemaFull",
    "assemble",
    "extensions_block",
    "l0_block",
    "model_blocks",
    "render",
    "write",
]

#: psql 变量名。调用方可覆盖: ``psql -v model_key=pw_xxx -f ...``。
PSQL_VAR = "model_key"

#: §18.5 要求头部注释写明 PostgreSQL 版本要求。依据是 DDL 里用了
#: - generated columns / partial index on a WHERE clause (9.0+)
#: - ``CREATE UNIQUE INDEX ... WHERE`` 配合 hypertable (9.5+)
#: - zhparser / pg_textsearch 需要 9.6+ 的 ``to_tsvector(regconfig, text)``
#: 取 14 作为下限: 板卡实际是 17, 留出余量。
MIN_POSTGRES_MAJOR = 14

#: §18.5 的五个分区名, 顺序即输出顺序。写死是为了让「文件里的分区」与
#: 「生成器的分区」不可能各排各的。
DDL_SECTIONS: tuple[str, ...] = (
    "extensions",
    "l0",
    "model",
    "hypertable",
    "rls",
    "triggers",
)


@dataclass(frozen=True)
class SchemaFull:
    """一次汇编的结果。分区顺序 = :data:`DDL_SECTIONS`。"""

    sections: dict[str, list[str]]

    def lines(self) -> list[str]:
        """展开成按分区顺序排列的语句列表。"""
        out: list[str] = []
        for name in DDL_SECTIONS:
            out.extend(self.sections.get(name, ()))
        return out


def extensions_block(*, with_timescale: bool = True) -> list[str]:
    """第 0 分区: 扩展。

    与 ``alembic/versions/0001_l0_base.py`` 的 ``CREATE EXTENSION`` 一致,
    但**不能**反过来只留这里: 迁移是真相源, 这里是它的镜像。两者不一致时
    以迁移为准, 由本模块的测试比对。

    ``with_timescale=False`` 时跳过 TimescaleDB —— 第 3 分区会连带跳过,
    5 张时序表退化为普通表 (仍可用, 只是失去自动分区)。
    """
    names = ["vector", "age", "pg_textsearch", "zhparser", "pg_trgm", "pgcrypto"]
    if with_timescale:
        # 扩展名是 timescaledb, 不是 timescale。见 storage/schema.py
        # OPTIONAL_EXTENSIONS 处的说明 —— `CREATE EXTENSION timescale`
        # 会报 "extension timescale is not available"。
        names.append("timescaledb")
    return [f"CREATE EXTENSION IF NOT EXISTS {name};" for name in names]


def l0_block(*, ini_path: Path | None = None) -> list[str]:
    """第 1 分区: L0 共享层 —— 取 alembic 离线输出。

    离线模式 (``--sql``) 不连库, 所以这一步不需要活的 DSN; 传一个
    占位 URL 即可 (alembic 只用它做方言推断)。真要连库的那份 DDL 由
    ``aterag-db upgrade`` 产出, 不在本文件里。
    """
    from alembic import command
    from alembic.config import Config

    ini = ini_path or _find_alembic_ini()
    buf = io.StringIO()
    # **必须**传 output_buffer, 不能只传 stdout。
    #
    # alembic 的离线 SQL 走 MigrationContext.output_buffer; 该值缺省是
    # None, 而 None 会落到 util.messaging.null_print_buffer —— 那个
    # 「缓冲器」其实是 write(sys.stdout)。只传 stdout=buf 的话 SQL 会打到
    # 控制台, 返回的 buf 是空的, 而 l0_block 照样返回空列表: 生成器看起来
    # 成功, 文件里却没有 L0 段。静默产出残缺文件比报错难查得多。
    cfg = Config(str(ini), output_buffer=buf)
    # alembic/env.py 的 _url() 在离线模式先看 -x dsn= 再看 sqlalchemy.url,
    # 这里走后者: 用 Python API 调 command.upgrade 时没有命令行参数可挂。
    cfg.set_main_option("sqlalchemy.url", "postgresql://offline/offline")
    command.upgrade(cfg, "head", sql=True)
    # 不滤掉 BEGIN;/COMMIT;: 它们是成对的, 滤掉一半会留下悬空事务。
    # psql 执行时保留原样即可。
    return _split(buf.getvalue())


def model_blocks(
    *,
    schema: str | None = None,
    psql_var: str | None = None,
) -> dict[str, list[str]]:
    """第 2/3/4 分区: 型号 schema、hypertable、RLS。

    ``schema`` 与 ``psql_var`` 二选一: 给前者出可直连执行的 DDL, 给后者
    出 psql 变量形式 (汇出 ``schema_full.sql`` 用)。都不给则默认 psql 形式。

    第 2 分区按 ``storage.model_schema`` 的批次分组输出, 使文件里的分组
    与实际建表顺序肉眼可对照 (§18.5 ②)。批次顺序不可换 ——
    ``alter_fk`` 批依赖前几批已建的表。
    """
    ref = schema_ref(schema) if schema else psql_schema_ref(psql_var or PSQL_VAR)
    batches = table_batches_for_ref(ref)
    # CREATE SCHEMA 由 model_ddl_for_ref 的第一条给出, 批次列表里没有它,
    # 所以这里单独取一次再与批次拼接。
    create_schema = model_ddl_for_ref(ref)[:1]
    model: list[str] = list(create_schema)
    for _name, group in batches:
        model.extend(group)
    return {
        "model": model,
        "hypertable": hypertable_ddl_for_ref(ref),
        "rls": rls_ddl_for_ref(ref),
        # 第 5 分区的触发器/视图分散在各表定义里 (update_tsv 触发器、
        # fact_as_of 视图), 已随第 2 分区输出, 这里显式记空以保持
        # 「五个分区都有交代」的可核对性。
        "triggers": [],
    }


def public_block() -> list[str]:
    """第 2 分区的一部分: public schema 的附件台账 (§3.9)。

    必须排在型号表**之前** —— ``doc.object_id`` 是指向
    ``public.document_object`` 的外键, 被引表不存在时建表直接失败。
    """
    return public_ddl()


#: ``CREATE EXTENSION IF NOT EXISTS <name>`` 的识别式。
_EXT_RE = re.compile(
    r"CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)


def _extension_names(stmts: list[str]) -> set[str]:
    """从一组语句里取出被创建的扩展名。

    按名字比而不是按语句全文比: alembic 会把 ``-- Running upgrade -> ...``
    注释挂在本段第一条语句前面, 于是那条语句的文本与手写清单里的裸语句
    对不上, ``vector`` 就会被输出两次。
    """
    out: set[str] = set()
    for stmt in stmts:
        # 注释行里可能出现 "extension" 字样, 先剥掉再看。
        body = "\n".join(
            ln for ln in stmt.splitlines() if not ln.strip().startswith("--")
        )
        for m in _EXT_RE.finditer(body):
            out.add(m.group(1).lower())
    return out


def assemble(
    *,
    schema: str | None = None,
    psql_var: str | None = None,
    with_timescale: bool = True,
    include_public: bool = True,
    ini_path: Path | None = None,
) -> SchemaFull:
    """汇编全部五个分区。

    第 0 分区会**去掉** L0 段里已建过的扩展。`extensions_block` 是手写
    清单, 而第 1 分区取自迁移 —— 两者必然有重叠 (0001 建了除 timescale
    外的全部必需扩展)。不去重的话每个扩展在文件里出现两次。

    靠「按语句文本去重」而不是维护一张「哪些扩展由迁移建」的表: 迁移是
    真相源, 它加了扩展而手写清单没加时, 手写清单少一条比多一条安全 ——
    多出来的 `CREATE EXTENSION IF NOT EXISTS` 靠幂等性兜住, 少掉的会
    直接建表失败。
    """
    l0 = l0_block(ini_path=ini_path)
    exts = extensions_block(with_timescale=with_timescale)
    # 按**扩展名**去重, 不按语句文本。alembic 会把 "-- Running upgrade -> ..."
    # 注释挂在本段第一条语句前面, 于是那条语句的文本是
    # ``'-- Running upgrade -> 0001_l0_base\n\nCREATE EXTENSION ... vector;'``,
    # 与手写清单里的裸语句对不上 —— vector 就会被输出两次。
    #
    # 保留条件是「这个扩展还没被 L0 段建过」, 即名字集合与 already **不相交**。
    # 写成 `names - already == set()` (无交集就保留) 会把逻辑整个反过来:
    # 留下重复项、丢掉 timescale, 而第 3 分区的 hypertable 随即建不出来。
    already = _extension_names(l0)
    exts = [s for s in exts if _extension_names([s]).isdisjoint(already)]

    sections: dict[str, list[str]] = {"extensions": exts, "l0": l0}
    sections.update(model_blocks(schema=schema, psql_var=psql_var))
    if include_public:
        # 插到 model 段最前: 附件表是 doc 的外键目标。
        sections["model"] = public_block() + sections["model"]
    return SchemaFull(sections=sections)


def render(
    result: SchemaFull,
    *,
    default_model_schema: str,
    with_timescale: bool = True,
) -> str:
    """渲染成可 ``psql -f`` 的完整文件文本。

    头尾都加了 ``\\if :{?model_key}`` 守卫: psql 的 ``\\set`` 会**覆盖**
    命令行 ``-v`` 传进来的同名变量, 所以不加守卫的话 ``-v model_key=...``
    会被文件里的默认悄悄盖掉, 而文件照样执行成功 —— 静默建错 schema。
    """
    head = [
        "-- ============================================================",
        "-- seed/schema_full.sql —— 自动生成, 请勿手工编辑 (docgen.ddl)",
        "--",
        "-- 来源章节: V6.0 §18.5 (五分区) + §3.5 / §3.9 / §5.8 / 第六章 /",
        "--           第十六章 / 第十七章 / §18.3.1 / 附录H",
        "-- 重新生成: python -m aterag.docgen.ddl",
        "--",
        f"-- PostgreSQL 要求: >= {MIN_POSTGRES_MAJOR}",
        f"-- TimescaleDB: {'已包含 (第 3 分区启用)' if with_timescale else '未包含 (第 3 分区跳过)'}",
        "-- L0 共享层真相源: alembic/versions/ (本文件第 1 分区由其离线产出)",
        "-- 型号 schema 真相源: src/aterag/storage/model_schema.py",
        "--",
        "-- 用法:",
        f"--   psql -v ON_ERROR_STOP=1 -v {PSQL_VAR}={default_model_schema} \\",
        "--        -f seed/schema_full.sql -d <db>",
        "--",
        f"-- 不传 -{PSQL_VAR}= 时用下面的默认值 (仅供演练, 正式部署必须显式传):",
        f"--   \\set {PSQL_VAR} '{default_model_schema}'",
        "-- ============================================================",
        "",
        # :{?var} 是 psql 的「变量是否已定义」测试。写成 :{var} (少个 ?)
        # 会变成「变量插值」—— 未定义时报错而不是走 \else 分支, 于是整个
        # 守卫失效。
        rf"\if :{{?{PSQL_VAR}}}",
        r"\else",
        f"\\set {PSQL_VAR} '{default_model_schema}'",
        r"\endif",
        "",
    ]
    body: list[str] = []
    titles = {
        "extensions": "0. 扩展",
        "l0": f"1. L0 共享 ({L0_SCHEMA}, 第六章 / §18.3.1)",
        "model": "2. 型号 schema (第三/五章 + 第十六/十七章 + 附录H)",
        "hypertable": "3. hypertable (TimescaleDB)",
        "rls": "4. RLS 策略 (§5.8)",
        "triggers": "5. 触发器与函数 (第九章; 随第 2 分区输出)",
    }
    for name in DDL_SECTIONS:
        stmts = result.sections.get(name, ())
        body.extend(
            [
                "",
                "-- ============================================================",
                f"-- {titles[name]}",
                "-- ============================================================",
            ]
        )
        if not stmts:
            body.extend(["-- (无)"])
            continue
        for sql in stmts:
            body.extend(["", sql.rstrip() if sql.rstrip().endswith(";") else sql.rstrip() + ";"])
    body.append("")
    return "\n".join(head + body)


def write(
    path: Path,
    *,
    default_model_schema: str,
    with_timescale: bool = True,
    ini_path: Path | None = None,
) -> Path:
    """生成并落盘 ``seed/schema_full.sql``。"""
    result = assemble(with_timescale=with_timescale, ini_path=ini_path)
    text = render(
        result,
        default_model_schema=default_model_schema,
        with_timescale=with_timescale,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _split(sql_text: str) -> list[str]:
    """把 alembic 的离线输出切成单条语句。

    复用 ``storage.model_schema._split_statements``: 它已处理 dollar-quoted
    块 (PL/pgSQL 函数体里有分号, 裸 ``split(';')`` 会切坏)。放在这里是
    为避免同一套切分逻辑出现第二个实现。
    """
    from ..storage.model_schema import _split_statements

    # 不滤掉 BEGIN;/COMMIT;: 它们是成对的, 滤掉一半会在文件里留下悬空的
    # COMMIT。保留原样还顺带让 L0 段在一个事务里建成, 失败不留半截结构。
    return _split_statements(sql_text)


def _find_alembic_ini() -> Path:
    """定位 alembic.ini (与 ``storage.cli._alembic_ini`` 同一策略)。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alembic.ini"
        if candidate.is_file() and (parent / "pyproject.toml").is_file():
            return candidate
    raise SystemExit(
        "找不到 alembic.ini (向上未找到同时含 alembic.ini 与 pyproject.toml 的目录)。\n"
        "  迁移只随源码检出分发; 作为 wheel 安装时应改用已生成的 seed/schema_full.sql。"
    )


def main() -> int:
    """``python -m aterag.docgen.ddl [输出路径] [默认型号 schema]``。"""
    import argparse

    ap = argparse.ArgumentParser(prog="aterag.docgen.ddl", description="生成 seed/schema_full.sql")
    ap.add_argument(
        "out",
        nargs="?",
        default="seed/schema_full.sql",
        help="输出路径 (默认 seed/schema_full.sql)",
    )
    ap.add_argument(
        "--default-model-schema",
        default="pw_example",
        help="不传 -v 时的默认型号 schema 名 (§18.5 的示例是 pw_sr5400)",
    )
    ap.add_argument(
        "--no-timescale",
        action="store_true",
        help="不含 TimescaleDB (第 3 分区跳过)",
    )
    args = ap.parse_args()
    p = write(
        Path(args.out),
        default_model_schema=args.default_model_schema,
        with_timescale=not args.no_timescale,
    )
    text = p.read_text(encoding="utf-8")
    print(f"已生成 {p} ({len(text.splitlines())} 行)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
