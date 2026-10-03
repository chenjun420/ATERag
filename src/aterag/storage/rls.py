"""行级安全 (RLS) 策略。

依据 V6.0:
    §5.8   RLS 与双时态叠加, RLS 是纵深防御
    §5.8.1 ctx_model() 与策略定义
    §5.8.3 三条纪律
    §18.2 存储层接口契约

核心判断: **RLS 是纵深防御, 不是唯一防线**。§5.8 开篇句说得很明确 ——
「schema 已把型号物理分开, RLS 作为纵深防御再兜一层」。第一层是 schema 物理隔离。

这个层次关系决定了两件事:
    1. 策略里的型号判断要用「schema 名」而不是「表里的 tenant 列」,
       因为 schema 本身已经保证了行不会跨型号。两层各管一件事:
       schema 管物理归属, RLS 管「当前连接有权看哪个 schema」。
    2. 策略生成为什么要生成器而不是模板 —— §18.5 的 DDL 汇编里每张表都要带
       策略, 手写 30 张表的 30 条 CREATE POLICY 必然漏, 而纪律 2 说
       「无 RLS 的表不允许上线」。

纪律 1 的实现后果 (重要): ctx_model() 读 current_setting, 而这个 setting
必须由认证中间件在连接建立时 SET。SQL 层不得自行解析 token。
本模块因此**不提供**「从 token 推导型号」的函数 —— 那属于认证层。
"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import SchemaError, model_schema_name, quote_ident, quote_literal

#: 上下文型号的 session setting 名。§5.8.1 的 current_setting('app.current_model')。
CTX_MODEL_SETTING = "app.current_model"

#: ctx_model() 的函数名。同一名, 建在哪个 schema 由 :func:`create_ctx_model_sql` 决定。
CTX_MODEL_FN = "ctx_model"


@dataclass(frozen=True)
class PolicySpec:
    """一条 RLS 策略的声明。

    分离「策略是什么」与「策略怎么生成 SQL」, 让 G 类门禁可以只检查前者 ——
    §18.9 的纪律 2 要求「无 RLS 的表不允许上线」, 而门禁要能枚举全部表与
    它们的策略, 不能靠解析 SQL 文本反推。
    """

    table: str
    policy_name: str
    #: 判定表达式。常见两种:
    #:   "schema_match"  -> ctx_model() = '<schema>'   (表内无型号列)
    #:   "column_match"  -> tenant_schema = ctx_model() (表内有型号列)
    kind: str
    #: column_match 时的列名
    column: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("schema_match", "column_match"):
            raise SchemaError(f"未知策略类型 {self.kind!r}, 只允许 schema_match / column_match")
        if self.kind == "column_match" and not self.column:
            raise SchemaError(f"column_match 策略必须给出 column, 表 {self.table}")


#: 必须走 column_match 的表 —— 它们存了跨型号的数据。
#:
#: 哪些表需要? 判据不是「表大」, 而是「同一张表里可能出现多个型号的行」。
#: 绝大多数型号 schema 内的表天然单型号 (schema 已经隔离了), 只有下面这些
#: 是刻意做成表内多行的:
#:   fact          —— §5.8.1 的示例策略正是它, 因为 fact 可能存共享事实
#:   provenance    —— 溯源链可能指向 L0 或别的型号的条目
#:   conflict      —— 冲突本身是跨型号的 (同一术语在两个型号有不同定义)
#: doc_chunk / test_case 用 schema_match, 与 §5.8.1 给出的示例一致。
COLUMN_MATCH_TABLES: frozenset[str] = frozenset({"fact", "provenance", "conflict"})

#: 型号 schema 内必须全部启用 RLS 的表。§5.8.1 只列了三个示例, 这里补全到
#: §18.5 汇编出来的实际表集。缺一个就在 G 门禁里报出来。
RLS_TABLES: tuple[str, ...] = (
    # 知识与事实
    "fact",
    "doc_chunk",
    "doc_clause",
    "test_case",
    "test_requirement",
    "test_condition",
    "provenance",
    "conflict",
    # 四遥
    "yx_point",
    "yc_point",
    "yk_command",
    "yt_parameter",
    # 保护定值
    "protection_setting",
    # 工装与工位
    "fixture",
    "test_station",
    "fixture_channel_map",
    "fixture_checkpoint",
)


class RlsError(RuntimeError):
    """RLS 配置失败。"""


def create_ctx_model_sql(schema: str = "public") -> str:
    """生成 ctx_model() 函数定义。§5.8.1 原样。

    ``SECURITY DEFINER`` 是必要的: RLS 策略会被表所有者之外的连接执行,
    而策略里要读 current_setting。STABLE 是必要的: 策略求值每行调用一次,
    VOLATILE 会让 PostgreSQL 认为结果随调用变化而拒绝做部分索引优化。

    ``missing_ok = true`` (current_setting 的第二个参数) 保证未设置时
    返回 NULL 而不是报错 —— 未设置就是无权限, 由策略表达式自然拒绝,
    而不是让整条查询抛异常。
    """
    return (
        f"CREATE OR REPLACE FUNCTION {quote_ident(schema)}.{quote_ident(CTX_MODEL_FN)}() "
        f"RETURNS TEXT AS $$ SELECT current_setting({quote_literal(CTX_MODEL_SETTING)}, true); $$ "
        f"LANGUAGE sql STABLE SECURITY DEFINER"
    )


def set_current_model_sql(model_key: str) -> str:
    """生成「设置当前型号」的 SQL。

    纪律 1: 只接受已认证层传入的型号键。**本函数不解析 token**,
    认证中间件在连接建立时调用它。型号键会进 SET 的字符串位置,
    所以先过一遍 validate_model_key (由 model_schema_name 内部完成)。
    """
    normalized = model_schema_name(model_key)
    return f"SET LOCAL {CTX_MODEL_SETTING} = {quote_literal(normalized)}"


def enable_rls_sql(schema: str, table: str) -> str:
    return f"ALTER TABLE {quote_ident(schema)}.{quote_ident(table)} ENABLE ROW LEVEL SECURITY"


def force_rls_sql(schema: str, table: str) -> str:
    """``FORCE ROW LEVEL SECURITY``。

    §5.8.1 只写了 ENABLE。但 ENABLE 有一个缺口: **表所有者绕过策略**。
    建表的角色 (通常是迁移角色, 也是连接角色) 天然绕过 RLS, 于是
    越权测试用同一个角色连就会「全部通过」——看起来隔离生效了, 其实没有。

    所以本模块一律 FORCE。代价是所有者也必须 SET app.current_model,
    这正是纪律 1 想要的效果: 没有任何路径能绕过。
    """
    return f"ALTER TABLE {quote_ident(schema)}.{quote_ident(table)} FORCE ROW LEVEL SECURITY"


def create_policy_sql(spec: PolicySpec, schema: str) -> str:
    """生成 CREATE POLICY 语句。§5.8.1 形态: FOR ALL USING + WITH CHECK。

    两者都要: USING 管读 (以及 UPDATE/DELETE 的目标行选择),
    WITH CHECK 管写。只写 USING 会让越权写入成功 —— 这是最容易被漏的一半。
    """
    if spec.kind == "schema_match":
        predicate = f"{CTX_MODEL_FN}() = {quote_literal(schema)}"
    else:
        assert spec.column is not None  # __post_init__ 已保证
        predicate = f"{quote_ident(spec.column)} = {CTX_MODEL_FN}()"

    return (
        f"CREATE POLICY {quote_ident(spec.policy_name)} "
        f"ON {quote_ident(schema)}.{quote_ident(spec.table)} "
        f"FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
    )


def default_specs(model_key: str) -> list[PolicySpec]:
    """为一个型号生成全套策略声明。

    顺序稳定 —— 生成的 DDL 因此可重现, 能进版本库做 diff。
    """
    specs: list[PolicySpec] = []
    for table in RLS_TABLES:
        if table in COLUMN_MATCH_TABLES:
            specs.append(
                PolicySpec(
                    table=table,
                    policy_name=f"{table}_model_isolation",
                    kind="column_match",
                    column="tenant_schema",
                )
            )
        else:
            specs.append(
                PolicySpec(
                    table=table,
                    policy_name=f"{table}_model_isolation",
                    kind="schema_match",
                )
            )
    return specs


def rls_ddl(model_key: str) -> list[str]:
    """生成一个型号的全部 RLS DDL。

    顺序: ENABLE + FORCE -> CREATE POLICY。两者顺序不敏感, 但 ENABLE 在前
    能让「建策略时报错」明确指向「表还没启用 RLS」, 而不是反过来让人怀疑
    策略写错了。
    """
    schema = model_schema_name(model_key)
    out: list[str] = []
    for spec in default_specs(model_key):
        out.append(enable_rls_sql(schema, spec.table))
        out.append(force_rls_sql(schema, spec.table))
        out.append(create_policy_sql(spec, schema))
    return out


def tables_without_rls_sql(schema: str) -> str:
    """生成「找出没有 RLS 的表」的查询。

    §5.8.3 纪律 2 的可执行版本: 无 RLS 的表不允许上线。
    门禁 (tests/gates) 跑这条查询, 结果非空即阻断。

    判据用 ``relrowsecurity`` 而不是「有没有 CREATE POLICY」——
    ENABLE ROW LEVEL SECURITY 是策略生效的前提; 有一堆策略但没启用
    等于没有。同理 FORCE 也要查, 理由见 force_rls_sql 的说明。
    """
    return (
        "SELECT c.relname "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = "
        f"{quote_literal(schema)} "
        "  AND c.relkind = 'r' "
        "  AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity)"
    )


def assert_no_unprotected_tables(cursor: object, schema: str) -> None:
    """CI 门禁: 目标 schema 下不允许存在未受 RLS 保护的表。

    ``cursor`` 是 psycopg cursor —— 与 schema.py 一样用 ``object`` 占位,
    避免本模块在无数据库驱动的环境里 import 失败。

    不排除的表: 全部。判据是「凡业务表必带 RLS」, 而不是列一张白名单。
    列白名单的问题是新加的表默认不在名单里, 于是新表静默裸奔 ——
    而新表恰恰是最需要保护的。
    """
    cursor.execute(tables_without_rls_sql(schema))  # type: ignore[attr-defined]
    rows = cursor.fetchall()  # type: ignore[attr-defined]
    if rows:
        names = ", ".join(r[0] for r in rows)
        raise RlsError(
            f"schema {schema!r} 下有 {len(rows)} 张表未启用 FORCE ROW LEVEL SECURITY: {names}。"
            " §5.8.3 纪律 2: 无 RLS 的表不允许上线。"
        )
