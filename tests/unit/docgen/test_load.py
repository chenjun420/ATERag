"""``docgen.load`` 的单元测试 —— CSV -> PostgreSQL 装载器。

重点测**编码规则**: CSV 的数组用 ``|`` 分隔、``dimension_vec`` 用空格分隔,
都不是 PostgreSQL 字面量。这三条规则写错时, ``psql`` 的报错都指向别处
(「有缺陷的数组常量」/「无效的类 numeric 输入语法」), 很难反推到编码本身。
所以这里**逐条钉死**。

另有两类静默失效特别值得盯:
1. **列名被当字面量** —— 生成出 ``string_to_array('var_refs','|')``, 每个公式
   插出同一个常量数组, 而行数与 AST 校验全都正常。
2. **空数组映成 NULL** —— ``var_refs`` 在表上是 ``NOT NULL``, 直接写就违反约束。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from aterag.docgen.load import count_rows, render_load_sql

#: 与 seed/formula.csv 逐字同构的最小表头。**必须包含那三个溯源列** ——
#: 它们是「CSV 有、表没有」的, 守卫要放行它们, 同时仍能挡住真正未知的列。
HEADER: tuple[str, ...] = (
    "formula_id",
    "name_zh",
    "name_zh_declared",
    "name_source",
    "name_en",
    "domain",
    "section",
    "domain_tags",
    "var_refs",
    "dimension_vec",
    "dimension_ok",
    "derive_from",
    "boundary",
    "confidence",
    "scope",
    "used_by_rule",
    "used_by_test",
    "used_by_axon",
    "errata",
    "source_ref",
    "source_kind",
    "expr_latex",
    "expr_plaintext",
    "expr_ascii",
    "expr_ast",
)

ROW: dict[str, str] = {
    "formula_id": "F_J.2.1_BUCK",
    "name_zh": "Buck 变换器",
    "name_zh_declared": "Buck 变换器",
    "name_source": "declared",
    "name_en": "",
    "domain": "J",
    "section": "J.2.1",
    "domain_tags": "变换器拓扑",
    "var_refs": "V_out|I_L",
    "dimension_vec": "2.0000 1.0000 -3.0000 -1.0000 0.0000 0.0000 0.0000",
    "dimension_ok": "true",
    "derive_from": "F_E.1",
    "boundary": "",
    "confidence": "0.95",
    "scope": "shared_l0",
    "used_by_rule": "R01|R02",
    "used_by_test": "",
    "used_by_axon": "",
    "errata": "E-1",
    "source_ref": "V6.0§J.2.1",
    "source_kind": "section",
    "expr_latex": "V_{out} = V_{in} D",
    "expr_plaintext": "V_out = V_in * D",
    "expr_ascii": "V_out = V_in * D",
    "expr_ast": '{"op": "="}',
}


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "formula.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(HEADER))
        writer.writeheader()
        writer.writerow(ROW)
    return path


def _select_block(sql: str) -> str:
    return sql[sql.index("INSERT INTO") :]


class TestColumnMapping:
    def test_provenance_columns_are_csv_only(self, csv_file: Path) -> None:
        """三个溯源列**刻意不入库**。

        它们的用途是让「name_zh 来自标准名还是方案原文」「source_ref 是不是真
        标准号」在 CSV 里可查, 而不是变成库里的第二份真相。所以表里没有它们,
        也不该报「未登记的列」。
        """
        sql = render_load_sql(csv_file)
        target_cols = sql[sql.index("INSERT INTO l0_term.formula (") : sql.index(")\nSELECT")]
        for column in ("name_zh_declared", "name_source", "source_kind"):
            assert column not in target_cols, f"{column} 不该进目标表"
            assert column in HEADER, f"{column} 必须留在 CSV 里"

    def test_unknown_column_is_rejected(self, csv_file: Path) -> None:
        """真正未知的列必须**报错**, 不能静默忽略。

        静默忽略的后果: 生成侧加了新列而生成器没跟上, 数据悄悄丢掉, 且文件照样
        生成成功。
        """
        text = csv_file.read_text(encoding="utf-8")
        csv_file.write_text(
            text.replace("formula_id,", "formula_id,brand_new_column,", 1), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="未登记的列"):
            render_load_sql(csv_file)

    def test_missing_column_is_rejected(self, csv_file: Path) -> None:
        """缺列也必须报错 —— 少一列会让 SELECT 引用不存在的列。"""
        text = csv_file.read_text(encoding="utf-8")
        csv_file.write_text(text.replace("errata,", "", 1), encoding="utf-8")
        with pytest.raises(ValueError, match="缺列"):
            render_load_sql(csv_file)


class TestArrayEncoding:
    def test_arrays_are_pipe_separated(self, csv_file: Path) -> None:
        """``var_refs`` 的 ``V_out|I_L`` -> ``string_to_array(..., '|')``。

        用 ``|`` 而不是 ``,``: CSV 自身的分隔符就是逗号, 用逗号会在文本里出现
        歧义, 且 PostgreSQL 侧无法区分「一个元素含逗号」与「两个元素」。
        """
        sql = _select_block(render_load_sql(csv_file))
        assert "COALESCE(string_to_array(NULLIF(var_refs, ''), '|'), '{}')::text[]" in sql

    def test_column_is_a_reference_not_a_literal(self, csv_file: Path) -> None:
        """列名必须**不带引号**。

        带引号会生成 ``string_to_array('var_refs','|')`` —— 一个常量, 于是每个
        公式都插出同一个数组。行数对、AST 对, 而 ``var_refs`` 全库同值。
        """
        sql = _select_block(render_load_sql(csv_file))
        assert "'var_refs'" not in sql
        assert "'domain_tags'" not in sql
        assert "'used_by_rule'" not in sql

    def test_empty_array_maps_to_empty_not_null(self, csv_file: Path) -> None:
        """空数组必须映成 ``'{}'``, **不能**是 NULL。

        ``var_refs`` / ``derive_from`` / ``domain_tags`` 在表上是 ``NOT NULL``,
        而 ``NULLIF(col, '')`` 把空串变成 NULL —— 直接写就违反约束。
        """
        sql = _select_block(render_load_sql(csv_file))
        assert "COALESCE(string_to_array(NULLIF(used_by_test, ''), '|'), '{}')" in sql

    def test_all_array_columns_are_converted(self, csv_file: Path) -> None:
        """六个数组列一个都不能漏 —— 漏一个就在 psql 上炸「有缺陷的数组常量」。"""
        sql = _select_block(render_load_sql(csv_file))
        for column in (
            "domain_tags",
            "var_refs",
            "derive_from",
            "used_by_rule",
            "used_by_test",
            "used_by_axon",
        ):
            assert f"string_to_array(NULLIF({column}, ''), '|')" in sql, column


class TestDimensionVector:
    def test_space_separated(self, csv_file: Path) -> None:
        """``dimension_vec`` 是**空格**分隔的 7 分量, 不是逗号。

        写成逗号会得到「无效的类 numeric 输入语法」—— 错误信息完全不像「分隔符
        搞错了」。
        """
        sql = _select_block(render_load_sql(csv_file))
        assert "regexp_split_to_array(btrim(dimension_vec), ' ')::numeric[]" in sql

    def test_precision_is_left_to_postgresql(self, csv_file: Path) -> None:
        """转成 ``numeric[]``, **不在 Python 里四舍五入**。

        表上是 ``NUMERIC(8,4)[]``, 让数据库判越界比自己算更可信 ——
        Python 端的 ``round`` 一旦与列精度不同步, 写进去的值就是错的, 而没有任何
        一方会报错。
        """
        sql = _select_block(render_load_sql(csv_file))
        assert "::numeric[]" in sql
        assert "round(" not in sql.lower()


class TestStaging:
    def test_staging_holds_every_csv_column(self, csv_file: Path) -> None:
        """staging 按 CSV **原文**建 25 列, 转换全留到 SELECT 里做。

        转换规则只有一处, 不会两处漂移。
        """
        sql = render_load_sql(csv_file)
        staging = sql[sql.index("CREATE TEMP TABLE stg (") : sql.index(");")]
        for column in HEADER:
            assert column in staging, f"staging 缺列 {column}"

    def test_copy_is_used_not_insert(self, csv_file: Path) -> None:
        """必须走 ``\\copy`` 进 staging —— 不能用 ``\\copy`` 直灌目标表。

        ``COPY`` 的列清单必须与文件列数**完全一致、不能跳过**, 而 CSV 25 列、
        目标表吃 22 列。直灌会报「最后期望字段后有额外数据」。
        """
        sql = render_load_sql(csv_file)
        assert "\\copy stg FROM" in sql
        copy_at = sql.index("\\copy stg FROM")
        insert_at = sql.index("INSERT INTO l0_term.formula")
        assert copy_at < insert_at

    def test_load_is_transactional(self, csv_file: Path) -> None:
        """``BEGIN`` / ``COMMIT`` 必须成对 —— 半截结构比建不出来更难查。"""
        sql = render_load_sql(csv_file)
        assert sql.count("BEGIN;") == sql.count("COMMIT;") == 1


class TestServerSidePath:
    def test_csv_location_overrides_local_path(self, csv_file: Path) -> None:
        """库在别的机器上时, ``\\copy`` 的路径由**服务器进程**读取。

        不给 ``csv_location`` 就会写入客户端的相对路径, 报「权限不够」——
        看着像权限问题, 实际是指错了机器。
        """
        sql = render_load_sql(csv_file, csv_location="/tmp/formula.csv")
        assert "'/tmp/formula.csv'" in sql
        assert "seed/formula.csv" not in sql


class TestCountRows:
    def test_uses_app_role_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """回读行数必须带 ``app.current_model``, 否则 RLS 下返回 0。

        「查不到」与「确实是 0 行」是两件事: 混起来就会把装载失败报成
        「装载了 0 条」。
        """
        seen: dict[str, str] = {}

        class _Proc:
            returncode = 0
            stdout = "102\n"

        def fake_run(cmd: list[str], **kw: object) -> _Proc:
            seen.update(kw["env"])  # type: ignore[arg-type]
            seen["cmd"] = " ".join(cmd)
            return _Proc()

        import subprocess

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert count_rows("postgresql://x", model_key="pw_test") == 102
        assert "app.current_model=pw_test" in seen["PGOPTIONS"]

    def test_failure_returns_none_not_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """查询失败返回 ``None``, **不返回 0**。

        返回 0 会让调用方以为「表是空的」, 从而把装载失败报成「装载了 0 条」。
        """
        import subprocess

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "permission denied"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
        assert count_rows("postgresql://x") is None
