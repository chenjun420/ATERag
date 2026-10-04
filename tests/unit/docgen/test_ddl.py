"""``docgen.ddl`` 的单元测试 (§18.5 全量 DDL 汇编)。

不需要数据库 —— 本模块只生成 SQL 文本。第 1 分区走 alembic 的**离线**
模式 (``--sql``), 同样不连库。

「生成物能不能真被 psql 跑」不在这里验: 那需要真库, 由
``tests/contract/test_schema_full_exec.py`` (marker=integration) 覆盖。
本文件的职责是**汇编器自身**的正确性: 分区齐全、顺序对、不重复、
参数化正确。
"""

from __future__ import annotations

import re

import pytest

from aterag.docgen.ddl import (
    DDL_SECTIONS,
    MIN_POSTGRES_MAJOR,
    PSQL_VAR,
    _extension_names,
    assemble,
    extensions_block,
    l0_block,
    model_blocks,
    render,
)
from aterag.storage.model_schema import MODEL_TABLES
from aterag.storage.rls import RLS_TABLES

DEFAULT_MODEL = "pw_sr5400"


@pytest.fixture(scope="module")
def result() -> object:
    return assemble()


@pytest.fixture(scope="module")
def text(result: object) -> str:
    return render(result, default_model_schema=DEFAULT_MODEL)


class TestSectionOrder:
    def test_all_five_sections_present(self, result: object) -> None:
        """§18.5 的五个分区一个都不能少, 名字也不能拼错。

        少一区是静默的: 文件照样能 psql 跑通, 只是少装了东西。
        """
        assert set(result.sections) == set(DDL_SECTIONS)

    def test_lines_follow_section_order(self, result: object) -> None:
        """sections() 按 DDL_SECTIONS 的顺序展开 —— 顺序决定文件里的执行顺序。

        判据用「区间不交叠」而不是「各自的首次出现位置」: 空分区 (第 5 分区)
        没有语句, 拿它去查位置会直接抛错, 而它恰恰是最需要被显式放行的那一区。
        """
        lines = result.lines()
        spans: list[tuple[str, int, int]] = []
        for name in DDL_SECTIONS:
            sts = result.sections.get(name, ())
            if not sts:
                continue
            idxs = [lines.index(s) for s in sts]
            spans.append((name, min(idxs), max(idxs)))
        for (na, _, a_end), (nb, b_start, _) in zip(spans, spans[1:], strict=False):
            assert a_end < b_start, f"分区 {na} 与 {nb} 交叠: {na} 到 {a_end}, {nb} 从 {b_start} 起"

    def test_l0_precedes_model(self, result: object) -> None:
        """§18.5 ②: 按「L0 共享 → 型号 schema」顺序输出。

        型号表的 concept_id 外键指向 l0_term.concept, 顺序反了建不出来。

        用分区下标而不是「含 CREATE TABLE 且不含 l0_term 的第一条」:
        alembic 会在 L0 段里建 ``alembic_version`` 表, 它既不含 l0_term
        也不属于型号 schema, 会被那个启发式误当成型号表。
        """
        lines = result.lines()
        l0_first = min(lines.index(s) for s in result.sections["l0"])
        model_first = min(lines.index(s) for s in result.sections["model"])
        assert l0_first < model_first


class TestVectorColumnTypes:
    """索引 opclass 必须与列类型一致。

    回归: ``formula_embedding.embedding`` 曾声明为 ``vector(1024)`` 而 HNSW
    索引用 ``halfvec_cosine_ops``, 板卡上直接报
    ``操作符表 "halfvec_cosine_ops" 不能处理数据类型 vector``。
    ``alembic upgrade head --sql`` 抓不到 —— 它不连库, 只产出文本。
    """

    def test_hnsw_opclass_matches_declared_column_type(self) -> None:
        l0 = l0_block()
        col_types: dict[tuple[str, str], str] = {}
        for stmt in l0:
            m = re.search(r"CREATE TABLE \S+\.(\w+) \((.*?)\n\s*\);", stmt, re.S)
            if not m:
                continue
            table, body = m.group(1), m.group(2)
            for line in body.splitlines():
                cm = re.match(r"\s*(\w+)\s+(halfvec|vector)\b", line)
                if cm:
                    col_types[(table, cm.group(1))] = cm.group(2)

        assert col_types, "未解析出任何向量列 —— 本测试会静默通过, 先查解析逻辑"

        checked = 0
        for stmt in l0:
            im = re.search(
                r"CREATE INDEX \S+ ON \S+\.(\w+) USING hnsw \((\w+) (\w+_ops)\)", stmt
            )
            if not im:
                continue
            table, col, opclass = im.groups()
            declared = col_types.get((table, col))
            assert declared is not None, f"{table}.{col} 建了 hnsw 索引但不是向量列"
            assert opclass.startswith(declared), (
                f"{table}.{col} 声明为 {declared}, 索引却用 {opclass} —— "
                "PostgreSQL 会报 DatatypeMismatch"
            )
            checked += 1
        assert checked, "没找到任何 hnsw 索引 —— 索引若被删本测试会空转"

    def test_formula_embedding_is_halfvec(self) -> None:
        """ADR-013: 部署维度统一 1024 halfvec, 不是 §18.3.1 字面的 vector(4096)。"""
        joined = "\n".join(l0_block())
        assert "embedding  halfvec(1024)" in joined
        assert "vector(4096)" not in joined


class TestL0FromMigrations:
    def test_l0_block_is_not_empty(self) -> None:
        """回归: 曾因 Config(stdout=buf) 而拿到空缓冲, 生成器「成功」但文件无 L0 段。

        alembic 的离线 SQL 走 output_buffer; 缺省会落到 null_print_buffer,
        也就是写到真 stdout。返回空列表不报错, 所以必须由测试兜住。
        """
        l0 = l0_block()
        assert l0, "L0 段为空 —— 检查 Config(output_buffer=...) 是否传了"
        assert any("l0_term.concept" in s for s in l0)

    def test_l0_contains_rule_tables(self) -> None:
        """0002 的 6 张表必须在 L0 段里 (ADR-018)。"""
        l0 = l0_block()
        for table in (
            "l0_term.rule",
            "l0_term.rule_parameter",
            "l0_term.rule_version",
            "l0_term.borrow_rule",
            "l0_term.jev_threshold",
            "l0_term.disambiguation_log",
        ):
            assert any(f"CREATE TABLE {table} (" in s for s in l0), table

    def test_transaction_is_balanced(self) -> None:
        """BEGIN; 与 COMMIT; 必须成对。

        曾实现里过滤掉 BEGIN; 而留着 COMMIT;, 文件里就留下悬空 COMMIT ——
        psql 会报「there is no transaction in progress」。
        """
        l0 = l0_block()
        begins = sum(1 for s in l0 if s.strip().upper() == "BEGIN;")
        commits = sum(1 for s in l0 if s.strip().upper() == "COMMIT;")
        assert begins == commits, f"BEGIN {begins} 次但 COMMIT {commits} 次"

    def test_formula_dimension_gate_travels_with_l0(self) -> None:
        """ADR-015 的触发器必须随 L0 段进文件, 否则 G1 在部署环境失效。"""
        l0 = l0_block()
        assert any("assert_formula_dimension_ok" in s for s in l0)

    def test_alembic_version_bookkeeping_is_stripped(self) -> None:
        """``alembic_version`` 是版本簿记, **不归** ``schema_full.sql`` 建。

        alembic 离线模式无条件发出 ``CREATE TABLE alembic_version``,
        重跑必炸(「关系 alembic_version 已经存在」), 而 ``ON_ERROR_STOP=1``
        会因此中止整个脚本 —— 后面 4 个分区一个都没跑, 表现为「退出码 3 但
        l0 表数 0」, 极难定位。簿记表无业务含义, 由 ``alembic stamp`` 负责。
        """
        l0 = l0_block()
        leftovers = [
            s for s in l0 if "alembic_version" in s and not s.strip().startswith("--")
        ]
        assert not leftovers, f"仍有未移除的簿记语句: {leftovers[:2]}"

    def test_removal_note_never_swallows_following_statement(self) -> None:
        """移除说明必须**以分号收尾**, 否则会吞掉紧随其后的语句。

        ``_split`` 按 ``;`` 切分, 而它对 ``--`` 注释毫无概念: 不带分号的注释行
        不算「已结束」, 下一条语句会被并进注释而丢失。实测后果是 ``COMMIT;``
        被吞 -> 文件里留下悬空事务 -> psql 报
        「there is no transaction in progress」。
        """
        l0 = l0_block()
        for index, statement in enumerate(l0):
            if not statement.lstrip().startswith("--"):
                continue
            # 纯注释行不算语句; 但若它单独成为一条(说明被切分器认可), 那它
            # 必须自带分号, 否则它吃掉了本该独立的后续语句。
            assert statement.rstrip().endswith(";"), (
                f"第 {index} 条注释没有以分号收尾: {statement[:60]!r}"
            )

    def test_business_tables_are_never_made_idempotent(self) -> None:
        """**只**动簿记表。业务表的 ``CREATE TABLE`` 必须原样保留。

        一刀切加 ``IF NOT EXISTS`` 会把「表已存在但结构是旧的」也当成功 ——
        绿地上建库时结构冲突必须炸出来, 而不是静默沿用旧表。
        """
        l0 = "\n".join(l0_block())
        for table in ("l0_term.formula", "l0_term.concept", "l0_term.rule"):
            assert f"CREATE TABLE {table} (" in l0, f"{table} 的建表语句被误改"


class TestExtensionDedup:
    def test_section_zero_only_adds_what_l0_lacks(self, result: object) -> None:
        """第 0 分区不得重复输出 L0 段已建的扩展。

        回归: 曾按语句文本去重, 但 alembic 把 "-- Running upgrade -> ..."
        注释挂在首条语句前, 于是 vector 漏网出现两次。
        """
        ext0 = _extension_names(result.sections["extensions"])
        ext_l0 = _extension_names(result.sections["l0"])
        assert not ext0 & ext_l0, f"第 0 分区重复建扩展: {sorted(ext0 & ext_l0)}"

    def test_timescaledb_survives_dedup(self, result: object) -> None:
        """回归: 去重条件写反 (``names - already == set()``) 会把 timescaledb 丢掉,
        而第 3 分区的 hypertable 随即建不出来 —— 文件仍能跑过前两分区,
        才在第 3 分区炸。

        扩展名是 ``timescaledb`` 不是 ``timescale``: 后者是产品名与 schema 名,
        没有这个扩展。板卡 192.168.5.25 实测
        ``pg_available_extensions WHERE name LIKE 'timescale%'`` 只返回
        ``timescaledb``。
        """
        assert "timescaledb" in _extension_names(result.sections["extensions"])

    def test_every_required_extension_appears_exactly_once(self, result: object) -> None:
        """七个必需扩展在文件里各出现一次。"""
        all_ext = _extension_names(result.sections["extensions"]) | _extension_names(
            result.sections["l0"]
        )
        assert all_ext == {
            "vector",
            "age",
            "pg_textsearch",
            "zhparser",
            "pg_trgm",
            "pgcrypto",
            "timescaledb",
        }

    def test_no_timescale_flag_drops_it(self) -> None:
        assert "timescaledb" not in _extension_names(extensions_block(with_timescale=False))

    def test_timescale_is_not_an_extension_name(self) -> None:
        """防回归: ``timescale`` 是产品名与 schema 名, **不是**扩展名。

        这条测试存在的理由: 本项目曾把可选扩展写成 ``timescale``, 单元测试
        也照抄了同一个错字, 于是测试全绿而 ``CREATE EXTENSION IF NOT EXISTS
        timescale`` 在板卡上报 "extension timescale is not available"。
        断言里若再抄一次代码的假设, 同样的错还会再犯 —— 所以这里断言的是
        「板卡实测的扩展名」, 并把实测命令写进注释。
        """
        names = _extension_names(extensions_block(with_timescale=True))
        assert "timescale" not in names, "不存在名为 timescale 的扩展"
        # 实测: SELECT name FROM pg_available_extensions WHERE name LIKE 'timescale%';
        #   板卡 192.168.5.25 (PG 17.11 + postgresql-17-timescaledb 2.30.2)
        #   -> 只有 timescaledb
        assert "timescaledb" in names

    def test_name_extraction_ignores_comment_lines(self) -> None:
        """注释里出现 extension 字样不得被当成建扩展。"""
        stmts = [
            "-- Running upgrade -> 0001_l0_base\n\nCREATE EXTENSION IF NOT EXISTS vector;",
            "-- 这里建 extension\nCREATE TABLE t (a int);",
        ]
        assert _extension_names(stmts) == {"vector"}


class TestPsqlVariable:
    def test_guard_uses_existence_test(self, text: str) -> None:
        r""":{?var} 才是 psql 的「变量是否已定义」测试。

        写成 :{var} (少个 ?) 会变成插值: 未定义时报错而不是走 \else 分支,
        守卫整体失效。
        """
        assert rf"\if :{{?{PSQL_VAR}}}" in text
        assert rf"\if :{{{PSQL_VAR}}}" not in text

    def test_guard_precedes_every_statement(self, text: str) -> None:
        """守卫必须在任何 DDL 之前 —— 之后才设变量就晚了。"""
        guard = text.index(rf"\if :{{?{PSQL_VAR}}}")
        first_ddl = text.index("CREATE EXTENSION")
        assert guard < first_ddl

    def test_default_model_schema_appears(self, text: str) -> None:
        assert f"\\set {PSQL_VAR} '{DEFAULT_MODEL}'" in text

    def test_usable_schema_name_arg(self) -> None:
        """直接给 schema 名时应出可直连执行的 DDL, 不含 psql 变量语法。

        断言找的是 ``:"model_key"`` 这个 psql 语法标记, 不能断言字符串
        ``model_key`` 不出现 —— ``doc`` / ``fixture`` / ``document_object``
        本来就有一个**同名列** ``model_key``, 那是业务字段不是变量插值。
        """
        blocks = model_blocks(schema="pw_pa601_d54a")
        joined = "\n".join(blocks["model"])
        assert '"pw_pa601_d54a".fact' in joined
        assert ':"model_key"' not in joined
        assert ":'model_key'" not in joined


class TestModelSection:
    def test_public_tables_precede_model_tables(self, result: object) -> None:
        """public 附件表必须先建 —— doc.object_id 是指向它的外键。

        匹配 ``.doc (`` 而不是 ``".doc"``: 默认是 psql 变量形式, 建表语句
        写作 ``:"model_key".doc (`` , 表名前没有紧贴的引号。
        """
        model = result.sections["model"]
        obj_at = next(i for i, s in enumerate(model) if "document_object" in s)
        doc_at = next(i for i, s in enumerate(model) if ".doc (" in s)
        assert obj_at < doc_at

    def test_every_model_table_created(self, result: object) -> None:
        text = "\n".join(result.sections["model"])
        for table in MODEL_TABLES:
            assert f'"{PSQL_VAR}"."' in text or f".{table} (" in text, table

    def test_rls_section_covers_all_rls_tables(self, result: object) -> None:
        """§5.8.3 纪律 2: 型号 schema 内每张业务表都要有策略。"""
        joined = "\n".join(result.sections["rls"])
        for table in RLS_TABLES:
            assert f"{table}_model_isolation" in joined, table

    def test_rls_uses_force(self, result: object) -> None:
        """ENABLE 不足 —— 表所有者会绕过策略。"""
        joined = "\n".join(result.sections["rls"])
        assert "FORCE ROW LEVEL SECURITY" in joined

    def test_hypertable_section_precedes_rls(self, result: object) -> None:
        """TimescaleDB 拆 chunk 后 FORCE RLS 要作用在父表, 故 hypertable 在前。"""
        lines = result.lines()
        hyper = next(i for i, s in enumerate(lines) if "create_hypertable" in s)
        policy = next(i for i, s in enumerate(lines) if s.startswith("CREATE POLICY"))
        assert hyper < policy


class TestRender:
    def test_header_records_required_provenance(self, text: str) -> None:
        """§18.5 ④: 头部注释要有生成方式、来源章节、PG 版本要求。"""
        assert "§18.5" in text
        assert f"PostgreSQL 要求: >= {MIN_POSTGRES_MAJOR}" in text
        assert "docgen.ddl" in text

    def test_every_statement_terminated(self, result: object) -> None:
        """分号缺失会在 psql 里被并进下一条 —— 静默错误。"""
        for name, stmts in result.sections.items():
            for sql in stmts:
                if sql.strip().upper() in ("BEGIN;", "COMMIT;"):
                    continue
                assert sql.rstrip().endswith(";"), f"{name} 段有未终止语句: {sql[:80]}"

    def test_triggers_section_is_explicit(self, result: object, text: str) -> None:
        """第 5 分区为空也要在文件里留一句话 —— 让人知道它不是漏了。"""
        assert result.sections["triggers"] == []
        assert "(无)" in text

    def test_update_tsv_trigger_present(self, result: object) -> None:
        """第 5 分区的触发器随第 2 分区输出 (分散在各表定义里), 不能真的没有。"""
        assert "trg_doc_chunk_tsv" in "\n".join(result.sections["model"])

    def test_fact_as_of_view_present(self, result: object) -> None:
        """§5.8.2 双时态视图 + §5.8.3 纪律 3「溯源导出必须走它」。"""
        assert "fact_as_of" in "\n".join(result.sections["model"])
