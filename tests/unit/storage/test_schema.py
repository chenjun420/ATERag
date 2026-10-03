"""storage.schema 的单元测试。

不需要数据库 —— 本模块的所有可测逻辑 (校验、折叠、引号) 都是纯函数。
数据库交互 (check_extensions / assert_extensions_installed) 由
tests/contract/test_storage_extensions.py 覆盖, 需要真库。
"""

from __future__ import annotations

import pytest

from aterag.storage.schema import (
    L0_SCHEMA,
    ExtensionStatus,
    SchemaError,
    assert_extensions_installed,
    check_extensions,
    create_l0_schema_sql,
    create_schema_sql,
    missing_required,
    model_schema_name,
    quote_ident,
    quote_literal,
    validate_model_key,
)


class TestValidateModelKey:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("PA601-D54A", "pa601-d54a"),
            ("pn1000-48a", "pn1000-48a"),
            ("A1", "a1"),
            ("0abc", "0abc"),
        ],
    )
    def test_accepts_and_lowercases(self, raw: str, expected: str) -> None:
        assert validate_model_key(raw) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # 空
            "-abc",  # 连字符开头
            "abc-",  # 连字符结尾
            "abc_def",  # 下划线
            "abc def",  # 空格
            "abc;drop",  # 语句分隔
            "abc'--",  # 引号注入
            "中文型号",
            "a" * 33,  # 超长
        ],
    )
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(SchemaError, match="型号键不合法"):
            validate_model_key(bad)


class TestModelSchemaName:
    @pytest.mark.parametrize(
        ("model_key", "expected"),
        [
            ("PA601-D54A", "pw_pa601_d54a"),
            ("PN1000-48A", "pw_pn1000_48a"),
            ("PN2000-24A", "pw_pn2000_24a"),
        ],
    )
    def test_folds_hyphen(self, model_key: str, expected: str) -> None:
        assert model_schema_name(model_key) == expected

    def test_prefix_is_pw(self) -> None:
        # §5.9 / ADR-003: 型号 schema 一律 pw_ 前缀。
        # 断言字面量而不是只断言前缀, 防止前缀被改成别的。
        assert model_schema_name("X1").startswith("pw_")

    def test_defence_in_depth_after_folding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """折叠后的二次校验必须真的拦得住。

        这条检查在当前 _MODEL_KEY_RE 下是不可达的死代码 —— 正则已保证
        折叠结果只含字母数字下划线。它防的是「将来有人放宽型号键正则」。

        既然不可达, 就不能靠「直觉上它会拦住」来保留: 用 monkeypatch 放开
        上游校验, 强制走通这条分支。将来若有人删掉二次校验, 本测试立刻红。
        """
        import aterag.storage.schema as schema_mod

        monkeypatch.setattr(schema_mod, "validate_model_key", lambda k: k)
        # 用含空格与引号的键, 不��「以数字开头」。加了 pw_ 前缀后
        # 结果必然以字母开头, 所以「以数字开头」这类输入不会触发本分支 ——
        # 能触发的只有「含非法字符」。
        for bad in ("a b", "a;b", "a'b"):
            with pytest.raises(SchemaError, match="折叠后不是合法 schema 名"):
                schema_mod.model_schema_name(bad)


class TestQuoteIdent:
    @pytest.mark.parametrize("name", ["public", "l0_term", "pw_pa601_d54a", "a_1"])
    def test_accepts_valid(self, name: str) -> None:
        assert quote_ident(name) == f'"{name}"'

    @pytest.mark.parametrize(
        "bad",
        ["Public", "1abc", "a-b", 'a"b', "", "a b", "x;DROP TABLE y"],
    )
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(SchemaError, match="标识符不合法"):
            quote_ident(bad)


class TestQuoteLiteral:
    def test_escapes_single_quote(self) -> None:
        assert quote_literal("O'Brien") == "'O''Brien'"

    def test_rejects_nul(self) -> None:
        # PostgreSQL text 存不了 U+0000, 放行会让数据库抛一个无关的编码错误。
        with pytest.raises(SchemaError, match="NUL"):
            quote_literal("abc\x00def")


class TestCreateSchemaSql:
    def test_idempotent(self) -> None:
        assert "IF NOT EXISTS" in create_schema_sql("pw_x")

    def test_l0_schema_name(self) -> None:
        assert L0_SCHEMA == "l0_term"
        assert create_l0_schema_sql() == create_schema_sql("l0_term")


class FakeCursor:
    """最小假 cursor, 只回答 check_extensions 的一条查询。"""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows
        self.queries: list[str] = []

    def execute(self, query: str, params: object = None) -> None:
        self.queries.append(query)

    def fetchall(self) -> list[tuple[str, str]]:
        return self._rows


class TestCheckExtensions:
    def test_reports_installed(self) -> None:
        cur = FakeCursor([("vector", "0.8.0"), ("age", "1.7.0")])
        statuses = check_extensions(cur)
        installed = {s.name: s.installed for s in statuses}
        assert installed["vector"] is True
        assert installed["age"] is True
        assert installed["zhparser"] is False

    def test_queries_pg_extension_not_available(self) -> None:
        """必须查 pg_extension (装没装), 不能查 pg_available_extensions (装得了吗)。

        §5.9 的教训: 只看可用扩展会得到「向量库可用」的假结论。
        """
        cur = FakeCursor([])
        check_extensions(cur)
        assert "FROM pg_extension" in cur.queries[0]
        assert "pg_available_extensions" not in cur.queries[0]

    def test_optional_missing_is_reported_not_required(self) -> None:
        cur = FakeCursor(
            [("vector", "0.8.0"), ("age", "1.7.0"), ("pg_textsearch", "1.4.0"), ("zhparser", "1.0")]
        )
        statuses = check_extensions(cur)
        ts = next(s for s in statuses if s.name == "timescaledb")
        assert ts.installed is False
        assert ts.required is False
        assert missing_required(statuses) == []


class TestMissingRequired:
    def test_empty_when_all_present(self) -> None:
        statuses = [
            ExtensionStatus(name=n, installed=True, version="1", required=True)
            for n in ("vector", "age", "pg_textsearch", "zhparser")
        ]
        assert missing_required(statuses) == []

    def test_lists_missing(self) -> None:
        statuses = [
            ExtensionStatus(name="vector", installed=True, version="1", required=True),
            ExtensionStatus(name="age", installed=False, version=None, required=True),
            ExtensionStatus(name="timescaledb", installed=False, version=None, required=False),
        ]
        assert missing_required(statuses) == ["age"]


class TestAssertExtensionsInstalled:
    ALL = [("vector", "0.8.0"), ("age", "1.7.0"),
           ("pg_textsearch", "1.4.0"), ("zhparser", "1.0")]

    def test_passes_when_all_present(self) -> None:
        cur = FakeCursor(list(self.ALL))
        statuses = assert_extensions_installed(cur)
        installed = {s.name: s.installed for s in statuses}
        assert installed["vector"] is True
        assert installed["timescaledb"] is False  # 可选, 不抛

    def test_raises_on_missing(self) -> None:
        cur = FakeCursor([("vector", "0.8.0")])
        with pytest.raises(SchemaError, match="缺少必需扩展"):
            assert_extensions_installed(cur)

    def test_message_names_all_missing(self) -> None:
        cur = FakeCursor([])
        with pytest.raises(SchemaError, match="age.*pg_textsearch.*zhparser"):
            assert_extensions_installed(cur)

    def test_message_warns_pgvector_is_wrong_name(self) -> None:
        """报错要指明 pgvector 是错的扩展名。

        deploy/native/04-init-postgres.sh:45 曾写成
        CREATE EXTENSION IF NOT EXISTS pgvector —— 该扩展不存在 (真名是
        vector), 在 ON_ERROR_STOP=1 下会中断整个脚本。修这个 bug 的人若
        只看到「extension does not exist」, 很可能会再去搜 pgvector 而
        找不到。所以错误信息直接给出正确答案。
        """
        cur = FakeCursor([])
        with pytest.raises(SchemaError, match="注意扩展名是 vector 不是 pgvector"):
            assert_extensions_installed(cur)

    def test_message_points_at_reference_script(self) -> None:
        cur = FakeCursor([])
        with pytest.raises(SchemaError, match="deploy/postgres/initdb/01-extensions.sql"):
            assert_extensions_installed(cur)
