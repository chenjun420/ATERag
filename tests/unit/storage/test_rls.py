"""storage.rls 的单元测试。

不需要数据库。本模块生成 SQL 字符串, 测试校验生成物与决策逻辑。
真正的「策略是否生效」需要真库, 由 tests/contract/test_rls_enforcement.py
覆盖 (那是 §18.4.2 W0 验收判据之二「RLS 越权访问测试全部拒绝」的落点)。
"""

from __future__ import annotations

import pytest

from aterag.storage.rls import (
    COLUMN_MATCH_TABLES,
    CTX_MODEL_SETTING,
    RLS_TABLES,
    PolicySpec,
    RlsError,
    assert_no_unprotected_tables,
    assert_schema_built,
    create_ctx_model_sql,
    create_policy_sql,
    default_specs,
    enable_rls_sql,
    force_rls_sql,
    rls_ddl,
    schema_tables_sql,
    set_current_model_sql,
    tables_without_rls_sql,
)
from aterag.storage.schema import SchemaError

MODEL = "PA601-D54A"
SCHEMA = "pw_pa601_d54a"


class TestCtxModelSql:
    def test_matches_spec_shape(self) -> None:
        """§5.8.1 的函数体: current_setting('app.current_model', true)。"""
        sql = create_ctx_model_sql()
        assert "current_setting('app.current_model', true)" in sql
        assert "LANGUAGE sql STABLE SECURITY DEFINER" in sql

    def test_missing_ok_is_true(self) -> None:
        """第二个参数必须是 true。

        未设置时返回 NULL 而非报错, 由策略表达式自然拒绝。未设置 = 无权限,
        这是纪律 1 想要的效果。
        """
        sql = create_ctx_model_sql()
        assert "'app.current_model', true)" in sql


class TestSetCurrentModelSql:
    def test_normalizes_to_schema_form(self) -> None:
        """SET 的是 schema 名而不是原始型号键。

        因为策略表达式是 ctx_model() = 'pw_<key>', 两边必须同形。
        """
        sql = set_current_model_sql(MODEL)
        assert "SET LOCAL app.current_model = 'pw_pa601_d54a'" in sql

    def test_uses_local_not_session(self) -> None:
        """必须是 SET LOCAL。

        SET (session 级) 会泄漏到该连接后续所有事务, 包括不属于当前请求的
        那些。连接池复用时这就是跨请求的越权。
        """
        assert "SET LOCAL" in set_current_model_sql(MODEL)

    def test_rejects_bad_model_key(self) -> None:
        with pytest.raises(SchemaError):
            set_current_model_sql("bad key; DROP TABLE x")


class TestPolicyGeneration:
    def test_schema_match_predicate(self) -> None:
        spec = PolicySpec(table="doc_chunk", policy_name="p", kind="schema_match")
        sql = create_policy_sql(spec, SCHEMA)
        assert "USING (ctx_model() = 'pw_pa601_d54a')" in sql
        assert "WITH CHECK (ctx_model() = 'pw_pa601_d54a')" in sql

    def test_column_match_predicate(self) -> None:
        spec = PolicySpec(
            table="fact", policy_name="p", kind="column_match", column="tenant_schema"
        )
        sql = create_policy_sql(spec, SCHEMA)
        assert 'USING ("tenant_schema" = ctx_model())' in sql

    def test_always_has_with_check(self) -> None:
        """只写 USING 会让越权写入成功。

        这是最容易被漏的一半: USING 管读, WITH CHECK 管写。
        """
        for spec in default_specs(MODEL):
            assert "WITH CHECK" in create_policy_sql(spec, SCHEMA), spec.table

    def test_always_for_all(self) -> None:
        for spec in default_specs(MODEL):
            assert "FOR ALL" in create_policy_sql(spec, SCHEMA), spec.table

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(SchemaError, match="未知策略类型"):
            PolicySpec(table="t", policy_name="p", kind="whatever")

    def test_column_match_requires_column(self) -> None:
        with pytest.raises(SchemaError, match="必须给出 column"):
            PolicySpec(table="t", policy_name="p", kind="column_match")


class TestDefaultSpecs:
    def test_covers_every_rls_table(self) -> None:
        tables = [s.table for s in default_specs(MODEL)]
        assert tables == list(RLS_TABLES)

    def test_no_duplicates(self) -> None:
        tables = [s.table for s in default_specs(MODEL)]
        assert len(tables) == len(set(tables))

    def test_column_match_tables_have_column(self) -> None:
        for spec in default_specs(MODEL):
            if spec.table in COLUMN_MATCH_TABLES:
                assert spec.kind == "column_match"
                assert spec.column == "tenant_schema"
            else:
                assert spec.kind == "schema_match"

    def test_order_is_stable(self) -> None:
        """顺序稳定 => 生成的 DDL 可重现, 能进版本库做 diff。"""
        assert [s.table for s in default_specs(MODEL)] == [s.table for s in default_specs(MODEL)]

    def test_includes_spec_example_tables(self) -> None:
        """§5.8.1 点名的三张表必须在列。"""
        for t in ("fact", "doc_chunk", "test_case"):
            assert t in RLS_TABLES


class TestRlsDdl:
    def test_enable_force_policy_per_table(self) -> None:
        ddl = rls_ddl(MODEL)
        assert len(ddl) == len(RLS_TABLES) * 3
        # 分号由 rls_ddl_for_ref 统一补: 单条生成器返回纯语句文本, 列表
        # 里的每一条都要能直接交给 psycopg / 写进 schema_full.sql。
        assert ddl[0] == enable_rls_sql(SCHEMA, RLS_TABLES[0]) + ";"
        assert ddl[1] == force_rls_sql(SCHEMA, RLS_TABLES[0]) + ";"

    def test_every_stmt_is_terminated(self) -> None:
        """漏一个分号就会在 psql 里被并进下一条 —— 且往往不报错。"""
        for stmt in rls_ddl(MODEL):
            assert stmt.rstrip().endswith(";"), stmt[:80]

    def test_emits_both_enable_and_force(self) -> None:
        """每张表一条 ENABLE + 一条 FORCE。

        ENABLE 单独用会被表所有者绕过。建表角色通常就是连接角色, 于是
        ENABLE 而不 FORCE 时越权测试用同一角色连会「全部通过」——
        看起来隔离生效了, 其实没有。
        """
        ddl = rls_ddl(MODEL)
        enables = [s for s in ddl if "ENABLE ROW LEVEL SECURITY" in s]
        forces = [s for s in ddl if "FORCE ROW LEVEL SECURITY" in s]
        assert len(enables) == len(RLS_TABLES)
        assert len(forces) == len(RLS_TABLES)
        for stmt in forces:
            assert "ENABLE" not in stmt

    def test_uses_model_schema(self) -> None:
        for stmt in rls_ddl(MODEL):
            assert SCHEMA in stmt


class FakeCursor:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows
        self.query: str | None = None

    def execute(self, query: str, params: object = None) -> None:
        self.query = query

    def fetchall(self) -> list[tuple[str]]:
        return self._rows


class TestNoUnprotectedTablesGate:
    def test_query_checks_both_rls_and_force(self) -> None:
        sql = tables_without_rls_sql(SCHEMA)
        assert "relrowsecurity" in sql
        assert "relforcerowsecurity" in sql
        assert "relkind = 'r'" in sql

    def test_passes_when_all_protected(self) -> None:
        cur = FakeCursor([])
        assert_no_unprotected_tables(cur, SCHEMA)

    def test_raises_and_names_tables(self) -> None:
        cur = FakeCursor([("secret_table",), ("other_secret",)])
        with pytest.raises(RlsError, match="secret_table, other_secret"):
            assert_no_unprotected_tables(cur, SCHEMA)

    def test_message_cites_discipline_two(self) -> None:
        cur = FakeCursor([("t1",)])
        with pytest.raises(RlsError, match="无 RLS 的表不允许上线"):
            assert_no_unprotected_tables(cur, SCHEMA)


class TestSchemaBuiltGate:
    """``assert_schema_built`` —— 防「空 schema 报通过」。

    这不是假想: 板卡首次部署时 ``init`` 因缺 l0_term 整体回滚, 留下两个
    空 ``pw_*`` schema, 而 ``assert_no_unprotected_tables`` 对空 schema
    恒真 (没有表就没有未保护的表), 门禁打印 [OK]。判据为空集时门禁无效。
    """

    def test_query_lists_tables(self) -> None:
        sql = schema_tables_sql(SCHEMA)
        assert "pg_class" in sql
        assert "relkind = 'r'" in sql
        assert SCHEMA in sql

    def test_empty_schema_raises(self) -> None:
        cur = FakeCursor([])
        with pytest.raises(RlsError, match="没有任何表"):
            assert_schema_built(cur, SCHEMA, expected=29)

    def test_empty_schema_message_names_the_trap(self) -> None:
        """错误信息必须点出「空 schema 会让 RLS 检查恒真」。"""
        cur = FakeCursor([])
        with pytest.raises(RlsError, match="恒真"):
            assert_schema_built(cur, SCHEMA, expected=29)

    def test_too_few_tables_raises(self) -> None:
        """建到一半失败并回滚时可能留下部分表 —— 数量不足也要报错。"""
        cur = FakeCursor([("doc",), ("clause",)])
        with pytest.raises(RlsError, match="预期至少 29 张"):
            assert_schema_built(cur, SCHEMA, expected=29)

    def test_passes_when_table_count_reached(self) -> None:
        rows = [(f"t{i}",) for i in range(29)]
        assert_schema_built(FakeCursor(rows), SCHEMA, expected=29)


def test_ctx_setting_name_is_single_sourced() -> None:
    """setting 名只在 CTX_MODEL_SETTING 定义一次。

    三处各写一遍字面量 (函数体 / SET / 策略文档) 时, 拼错一个字符的后果是
    策略静默失效 —— NULL 比较恒为假, 所有行被拒, 而不是被放行。
    """
    assert CTX_MODEL_SETTING == "app.current_model"
    assert CTX_MODEL_SETTING in create_ctx_model_sql()
    assert CTX_MODEL_SETTING in set_current_model_sql(MODEL)
