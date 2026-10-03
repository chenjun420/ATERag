"""``storage.model_schema`` 的单元测试。

不需要数据库 —— 本模块只生成 SQL 字符串。

「生成的 DDL 能不能真跑」不在这里验: 那需要真库, 由
``tests/contract/test_model_schema_ddl.py`` (marker=integration) 覆盖。
本文件的职责是**生成器自身**的正确性: 表集完整、批间依赖正确、
与 §5.8 RLS 纪律一致。
"""

from __future__ import annotations

import re

import pytest

from aterag.storage.model_schema import (
    HYPERTABLES,
    MODEL_TABLES,
    PUBLIC_TABLES,
    hypertable_available_sql,
    hypertable_ddl,
    model_ddl,
    model_schema_ddl,
    public_ddl,
    table_batches,
)
from aterag.storage.rls import (
    COLUMN_MATCH_TABLES,
    RLS_TABLES,
    TENANT_COLUMN,
    rls_ddl,
)
from aterag.storage.schema import L0_SCHEMA, SchemaError

MODEL = "PA601-D54A"
SCHEMA = "pw_pa601_d54a"
OTHER_MODEL = "PN2000-24A"
OTHER_SCHEMA = "pw_pn2000_24a"

#: DDL 里 schema 名是**带引号**的标识符。走 ``quote_ident`` 渲染是为了让
#: 同一个生成器能同时输出真实 schema 名与 psql 变量形式 (``:\"model_key\"``,
#: §18.5 ③) —— 后者的引号是 psql 语法的一部分, 不是我们加的。
#: 见 storage/rls.py 的 SchemaRef。
QSCHEMA = f'"{SCHEMA}"'


@pytest.fixture(scope="module")
def stmts() -> list[str]:
    return model_ddl(MODEL)


class TestTableCoverage:
    def test_every_registered_table_is_created(self, stmts: list[str]) -> None:
        """MODEL_TABLES 里登记的表, DDL 里必须真的建出来。"""
        text = "\n".join(stmts)
        created = set(re.findall(rf"CREATE TABLE {QSCHEMA}\.(\w+)", text))
        assert created == set(MODEL_TABLES)

    def test_no_unregistered_table_is_created(self, stmts: list[str]) -> None:
        """反向: DDL 里建的表必须在 MODEL_TABLES 登记。

        没有这条, 有人加了表却忘了登记, 门禁 (RLS 覆盖检查) 就会漏掉它。
        """
        text = "\n".join(stmts)
        created = set(re.findall(rf"CREATE TABLE {QSCHEMA}\.(\w+)", text))
        assert not created - set(MODEL_TABLES)

    def test_table_count_meets_spec_floor(self) -> None:
        """§18.5 验收: 每型号表数 >= 30。"""
        assert len(MODEL_TABLES) >= 30, (
            f"型号 schema 只有 {len(MODEL_TABLES)} 张表, §18.5 要求 >= 30。"
            " 少于 30 时 RLS 门禁的覆盖面也不足。"
        )

    def test_rls_tables_all_exist(self, stmts: list[str]) -> None:
        """storage.rls.RLS_TABLES 里的表必须真的被建出来。

        这是两条独立的清单 (一处管策略生成, 一处管 DDL), 靠本测试绑在一起。
        若 rls.py 列了一张本模块不建的表, RLS DDL 会指向不存在的表而建库
        直接失败 —— 与其失败在部署时, 不如失败在单测里。
        """
        text = "\n".join(stmts)
        created = set(re.findall(rf"CREATE TABLE {QSCHEMA}\.(\w+)", text))
        missing = set(RLS_TABLES) - created
        assert not missing, f"rls.py 为不存在的表生成策略: {sorted(missing)}"


class TestHypertables:
    def test_spec_tables_are_hypertables(self) -> None:
        """§18.5 第 3 分区点名三张表; §3.5.5 另有两张台账表。"""
        named = {tbl for tbl, _ts, _iv in HYPERTABLES}
        assert {"yx_soe", "yc_trend", "protection_action"} <= named
        assert {"instrument_ledger", "fixture_tp_probe"} <= named

    def test_every_hypertable_target_is_created(self, stmts: list[str]) -> None:
        text = "\n".join(stmts)
        created = set(re.findall(rf"CREATE TABLE {QSCHEMA}\.(\w+)", text))
        named = {tbl for tbl, _ts, _iv in HYPERTABLES}
        assert not named - created

    def test_interval_is_spec_value(self) -> None:
        """chunk 间隔照抄 spec, 不自行调优 —— 调优需要压测依据。"""
        got = {tbl: iv for tbl, _ts, iv in HYPERTABLES}
        assert got["yx_soe"] == "1 day"
        assert got["yc_trend"] == "7 days"
        assert got["protection_action"] == "7 days"

    def test_available_sql_checks_installed_not_available(self) -> None:
        """必须查 pg_extension (装没装), 不是 pg_available_extensions (装得了吗)。"""
        sql = hypertable_available_sql()
        assert "pg_extension" in sql
        assert "pg_available_extensions" not in sql

    def test_hypertable_ddl_uses_event_time_for_soe(self) -> None:
        """yx_soe 的分区键是 event_time, 不是 capture_time/store_time。

        断言写全限定名: create_hypertable 收的第一个参数是
        ``'<schema>.<table>'`` 的**字符串**, 不带引号就会被当成裸表名。
        """
        stmts = hypertable_ddl(MODEL)
        assert any(f"'{SCHEMA}.yx_soe', 'event_time'" in s for s in stmts)
        # 反向: 三个时标里只有 event_time 能当分区键 (它才是物理发生时刻)。
        soe = next(s for s in stmts if f"'{SCHEMA}.yx_soe'" in s)
        assert "capture_time" not in soe
        assert "store_time" not in soe


class TestBatchOrder:
    def test_alter_fk_is_last(self) -> None:
        """补 FK 的 ALTER 必须最后 —— 否则被引表还没建。"""
        batches = [name for name, _ in table_batches()]
        assert batches[-1] == "alter_fk"

    def test_batches_are_named_for_spec_sections(self) -> None:
        names = [name for name, _ in table_batches()]
        assert names[:5] == ["knowledge", "telemetry", "protection", "fixture", "instrument"]

    def test_all_stmts_are_terminated(self, stmts: list[str]) -> None:
        """每条语句必须以分号结尾, 否则拼接执行时会被并进下一条。"""
        for s in stmts:
            assert s.rstrip().endswith(";"), f"未以分号结尾: {s[:80]}"

    def test_create_schema_is_first(self, stmts: list[str]) -> None:
        """CREATE SCHEMA 必须在最前, 且带分号 (见 model_ddl 里的说明)。"""
        assert stmts[0] == f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}";'


class TestForeignKeys:
    def test_alter_fks_reference_existing_tables(self, stmts: list[str]) -> None:
        text = "\n".join(stmts)
        created = set(re.findall(rf"CREATE TABLE {QSCHEMA}\.(\w+)", text))
        alters = re.findall(
            rf"ALTER TABLE {QSCHEMA}\.\w+ ADD CONSTRAINT \S+ "
            rf"FOREIGN KEY \(\w+\) REFERENCES {QSCHEMA}\.(\w+)",
            text,
        )
        assert alters, "一条 ALTER FK 都没有 —— 生成器坏了"
        assert not set(alters) - created, "ALTER 指向不存在的表"

    def test_hypertables_have_no_outgoing_fk(self, stmts: list[str]) -> None:
        """hypertable 不加出边外键 (TimescaleDB 限制, 且静默不生效更危险)。

        见 model_schema.py 里「刻意不加外键的地方」第 1 条。
        """
        hypertable_names = {tbl for tbl, _ts, _iv in HYPERTABLES}
        text = "\n".join(stmts)
        alters = re.findall(
            rf"ALTER TABLE {QSCHEMA}\.(\w+) ADD CONSTRAINT \S+ FOREIGN KEY",
            text,
        )
        assert not set(alters) & hypertable_names

    def test_l0_foreign_keys_are_present(self, stmts: list[str]) -> None:
        """四遥/工装表的 concept_id 必须 REFERENCES l0_term.concept。

        §5.10.3 断言 3 说「所有 fact 的 concept_id 必须在 L0 术语表中存在」;
        同一纪律对点表成立 (SHACL Shape 10)。
        """
        text = "\n".join(stmts)
        assert text.count(f"REFERENCES {L0_SCHEMA}.concept(concept_id)") >= 5

    def test_fact_tenant_schema_defaults_to_own_schema(self, stmts: list[str]) -> None:
        """§5.8.1 的 fact 策略是 column_match(tenant_schema = ctx_model()),
        所以 DEFAULT 必须等于本 schema 名, 否则漏填的行使策略判 false。
        """
        text = "\n".join(stmts)
        assert f"DEFAULT '{SCHEMA}'" in text


class TestColumnMatchTables:
    """column_match 的表必须真的有型号列。

    回归: ``COLUMN_MATCH_TABLES`` 曾含 ``provenance`` / ``conflict``, 理由是
    「溯源链跨型号」「冲突跨型号」。那是把「行可以引用别的型号的对象」误当成
    「同一张表里存着多个型号的行」—— 两者不是一回事。那两张表的 DDL
    (§3.5.3) 里没有 ``tenant_schema`` 列, 板卡上 CREATE POLICY 直接报
    「字段 "tenant_schema" 不存在」。

    策略生成与 DDL 生成是两条独立路径, 靠常量耦合; 本测试就是那条耦合的
    对账, 少一个 column_match 表多一列都会被抓到。
    """

    def test_column_match_tables_declare_the_column(self, stmts: list[str]) -> None:
        text = "\n".join(stmts)
        for table in COLUMN_MATCH_TABLES:
            body = re.search(rf'CREATE TABLE "{re.escape(SCHEMA)}"\.{table} \(.*?\n\);', text, re.S)
            assert body, f"{table} 被 RLS 声明要保护, 但 DDL 里没有这张表"
            assert TENANT_COLUMN in body.group(0), (
                f"{table} 走 column_match 但 DDL 里没有 {TENANT_COLUMN} 列 —— "
                "CREATE POLICY 会报 UndefinedColumn"
            )

    def test_only_fact_uses_column_match(self) -> None:
        """§5.8.1 的示例策略只涉及 fact; §3.5.3 里也只有 fact 有该列。"""
        assert COLUMN_MATCH_TABLES == frozenset({"fact"})

    def test_spec_column_matches_spec_ddl(self) -> None:
        """列名与 §3.5.3 fact DDL 逐字一致 —— 改了要连 DDL 一起改。"""
        assert TENANT_COLUMN == "tenant_schema"

    def test_policy_uses_the_constant(self) -> None:
        """策略里不能硬写列名, 否则改常量时策略静默失配。

        列名是**带引号**的标识符 (``quote_ident``), 所以断言要带引号 ——
        写不带引号的匹配会「看起来在断言常量」, 实际永远匹配不上。
        """
        joined = "\n".join(rls_ddl(MODEL))
        assert f'"{TENANT_COLUMN}" = ctx_model()' in joined


class TestSchemaIsolation:
    def test_two_models_get_distinct_schemas(self) -> None:
        a = model_ddl(MODEL)
        b = model_ddl(OTHER_MODEL)
        assert f'"{OTHER_SCHEMA}".fact' in "\n".join(b)
        assert f"{QSCHEMA}.fact" not in "\n".join(b)
        assert f"{QSCHEMA}.fact" in "\n".join(a)

    def test_no_unqualified_model_table_references(self, stmts: list[str]) -> None:
        """型号表之间的引用必须全限定 —— 不依赖 search_path。"""
        for s in stmts:
            # 允许出现的裸表名只限 l0_term.* 与 public 的附件表。
            assert "FROM fact" not in s
            assert "JOIN fact" not in s


class TestSpecFidelity:
    def test_r17_timeout_constraint_present(self, stmts: list[str]) -> None:
        """R17 的结构性落地: 超时 > N × 心跳。"""
        text = "\n".join(stmts)
        assert "ck_timeout_enough" in text
        assert "heartbeat_n * heartbeat_period_ms" in text

    def test_p13_exactly_one_target_present(self, stmts: list[str]) -> None:
        """§17.3.1 P13: 一个通道恰好映射一个点位。"""
        text = "\n".join(stmts)
        assert "ck_exactly_one_target" in text

    def test_c8_trip_below_absmax_present(self, stmts: list[str]) -> None:
        """§16.6.3 C8: 定值不得逾越器件绝对最大 (R3/R10)。"""
        assert "ck_trip_below_absmax" in "\n".join(stmts)

    def test_alarm_in_range_present(self, stmts: list[str]) -> None:
        """§16.3.2: 阈值超量程会让告警永不触发, 必须有 CHECK。"""
        assert "ck_alarm_in_range" in "\n".join(stmts)

    def test_reproducible_constraint_present(self) -> None:
        """§3.9.3: 声称可重建就必须给出上游来源。"""
        text = "\n".join(public_ddl())
        assert "ck_reproducible" in text

    def test_embedding_dim_follows_adr_013(self, stmts: list[str]) -> None:
        """统一 halfvec(1024), 不是 §18.3.1 字面的 vector(4096)。"""
        assert "halfvec(1024)" in "\n".join(stmts)
        assert "vector(4096)" not in "\n".join(stmts)


class TestDerivedTables:
    """doc / clause / trace —— 列集由本项目推导, 不是 spec 原文 (ADR-019)。

    推导出来的列必须由测试守住, 否则改列时没人知道哪一列有约束力。
    """

    def test_clause_is_the_only_source_of_clause_uid(self, stmts: list[str]) -> None:
        """§17.4 硬约束: 不得编造条款号, clause_uid 必须来自 clause 表。

        落实方式是让**每一张**带 clause_uid 的表都外键到 clause。
        加一列新表带 clause_uid 却忘了外键, 这条测试就红。
        """
        text = "\n".join(stmts)
        carriers = sorted(
            set(re.findall(rf"CREATE TABLE {QSCHEMA}\.(\w+)", text))
            & {
                "yx_point",
                "yc_point",
                "yk_command",
                "yt_parameter",
                "protection_setting",
                "fixture_channel_map",
                "comm_protocol",
            }
        )
        assert carriers, "带 clause_uid 的表全没了 —— clause 链断裂"
        for table in carriers:
            body = re.search(rf"CREATE TABLE {QSCHEMA}\.{table} \(.*?\n\);", text, re.S)
            assert body, table
            # 逐表断言, 不能只查整份文本: 否则「有一张表接对了」会被
            # 误判成「七张表都接对了」。
            assert (
                f"REFERENCES {QSCHEMA}.clause(clause_uid)" in body.group(0)
            ), f"{table}.clause_uid 未外键到 clause —— §17.4「不得编造条款号」失效"

    def test_doc_primary_key_is_doc_id_plus_rev(self, stmts: list[str]) -> None:
        """文档换版是「多一版」, 所以 rev 进主键。

        rev 不进主键的话, 换版就只能 UPDATE 覆盖, 历史版本永远查不到。
        """
        text = "\n".join(stmts)
        assert re.search(rf"CREATE TABLE {QSCHEMA}\.doc \(.*?PRIMARY KEY \(doc_id, rev\)", text, re.S)

    def test_doc_has_at_most_one_current_rev(self, stmts: list[str]) -> None:
        """partial unique index 与 bitemporal.py 同一机制。"""
        text = "\n".join(stmts)
        assert (
            f"CREATE UNIQUE INDEX ux_doc_current ON {QSCHEMA}.doc (doc_id) "
            f"WHERE valid_until IS NULL" in text
        )

    def test_doc_object_id_points_at_public_ledger(self, stmts: list[str]) -> None:
        """§3.9 document_object 是物理附件台账, doc 是逻辑文档台账。

        两者不是一回事, 所以是**可选**外键 (规格书原件未入库时可空),
        不是主键也不是 NOT NULL。
        """
        text = "\n".join(stmts)
        assert "REFERENCES public.document_object(object_id)" in text
        assert re.search(r"object_id\s+BIGINT REFERENCES", text)

    def test_clause_has_no_time_columns(self, stmts: list[str]) -> None:
        """装饰列判据 (ADR-019 决策 2)。

        clause_uid 单列主键 + valid_from/valid_until ⟹ 两个时间列写一次就
        冻结, 反而给人「这张表能时间旅行」的错觉。修订版编码在 clause_uid
        里 (约定形如 ``SR-<model>-<sr>@<rev>#<path>``), 换版产生新 uid。
        """
        text = "\n".join(stmts)
        body = re.search(rf"CREATE TABLE {QSCHEMA}\.clause \(.*?\n\);", text, re.S)
        assert body
        assert "valid_from" not in body.group(0)
        assert "valid_until" not in body.group(0)

    def test_clause_uid_shape_is_not_enforced(self, stmts: list[str]) -> None:
        """刻意**不**对 clause_uid 加格式 CHECK。

        编号规则会随客户文档变 (§17.4 只说「必须来自 clause 表」, 没规定
        长什么样)。在 DDL 里钉死格式, 等于让第一个不按此格式的客户规格书
        直接建不出条款行 —— 比格式不统一糟得多。
        """
        text = "\n".join(stmts)
        body = re.search(rf"CREATE TABLE {QSCHEMA}\.clause \(.*?\n\);", text, re.S)
        assert body
        assert "clause_uid ~" not in body.group(0)

    def test_clause_foreign_key_to_doc(self, stmts: list[str]) -> None:
        """条款必须挂在某个文档的某个修订版上 —— 否则 clause_uid 无法解析。"""
        text = "\n".join(stmts)
        assert (
            f"FOREIGN KEY (doc_id, rev) REFERENCES {QSCHEMA}.doc (doc_id, rev) "
            f"ON DELETE CASCADE" in text
        )

    def test_trace_layer_has_exactly_six_values(self, stmts: list[str]) -> None:
        """§1.6 六级链: 公理 -> 定理 -> 公式 -> 规则 -> 测试 -> 判据。

        六个值用**有序枚举**而非六张表: 换一级要改 CHECK 而不是改 schema。
        """
        text = "\n".join(stmts)
        body = re.search(rf"CREATE TABLE {QSCHEMA}\.trace \(.*?CHECK \(layer IN \(.*?\)\)", text, re.S)
        assert body, "trace 缺 layer 约束"
        assert re.search(
            r"'axiom','theorem','formula','rule','test','judgement'", body.group(0)
        )

    def test_trace_rejects_self_loop(self, stmts: list[str]) -> None:
        """自环的推导链会让「反查上游」死循环。"""
        assert "ck_trace_self_ref" in "\n".join(stmts)


class TestPublicTables:
    def test_document_tables_exist(self) -> None:
        text = "\n".join(public_ddl())
        for t in PUBLIC_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {t} " in text

    def test_document_tables_are_idempotent(self) -> None:
        """public_ddl 走 IF NOT EXISTS —— 反复 init 不得报错。"""
        text = "\n".join(public_ddl())
        assert "IF NOT EXISTS document_object" in text
        assert "IF NOT EXISTS document_page" in text


class TestModelSchemaDdl:
    def test_order_is_tables_then_hypertable_then_rls(self) -> None:
        """§18.5 分区序: 2 型号表 -> 3 hypertable -> 4 RLS。

        RLS 必须在 hypertable 之后: TimescaleDB 把 hypertable 拆成子表,
        FORCE RLS 要作用在父表才覆盖全部 chunk。
        """
        full = model_schema_ddl(MODEL)
        first_hyper = next(i for i, s in enumerate(full) if "create_hypertable" in s)
        first_policy = next(i for i, s in enumerate(full) if s.startswith("CREATE POLICY"))
        last_table = max(
            i for i, s in enumerate(full) if s.startswith("CREATE TABLE")
        )
        assert last_table < first_hyper < first_policy

    def test_includes_force_rls(self) -> None:
        """ENABLE 不足 —— 表所有者会绕过策略。"""
        full = "\n".join(model_schema_ddl(MODEL))
        assert "FORCE ROW LEVEL SECURITY" in full
        assert "ENABLE ROW LEVEL SECURITY" in full


class TestInvalidModelKey:
    @pytest.mark.parametrize("bad", ["", "a" * 40, "drop table", "a;b", "模型A"])
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(SchemaError):
            model_ddl(bad)
