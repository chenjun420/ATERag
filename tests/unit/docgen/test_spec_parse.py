"""``docgen.spec_parse`` 的单元测试。

不需要数据库, 也不需要读方案全文 —— 用内联的最小 Markdown 片段驱动解析器。
「真实语料的覆盖数字」由 :func:`test_corpus_reaches_the_w1_floor` 单独断言,
它读方案文件, 故标记为 slow。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aterag.docgen.spec_parse import (
    FORMULA_ID_RE,
    parse_formula_rows,
)

SPEC = Path(__file__).resolve().parents[3] / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md"

GOOD = """\
### J.2 三大基本变换器电压转换比

| 公式 ID | 名称 | 表达式 | 量纲 | 上游 |
|---|---|---|---|---|
| `F_J.2.1_BUCK` | Buck 变换比 | `V_out = D · V_in` | `[V]` | A-2, A-4, T3 |
| `F_J.2.2_BOOST` | Boost 变换比 | `V_out = V_in / (1 − D)` | `[V]` | A-2, T3 |
"""


class TestBasicExtraction:
    def test_finds_both_rows(self) -> None:
        rep = parse_formula_rows(GOOD.splitlines())
        assert [r.formula_id for r in rep.rows] == ["F_J.2.1_BUCK", "F_J.2.2_BOOST"]

    def test_reads_all_roles(self) -> None:
        row = parse_formula_rows(GOOD.splitlines()).rows[0]
        assert row.expression == "V_out = D · V_in"
        assert row.dimension_text == "[V]"
        assert row.upstream == ("A-2", "A-4", "T3")
        assert row.name_zh == "Buck 变换比"

    def test_domain_and_section_come_from_heading_and_id(self) -> None:
        row = parse_formula_rows(GOOD.splitlines()).rows[0]
        assert row.domain == "J"
        assert row.section is not None and "J.2" in row.section

    def test_separator_row_is_not_data(self) -> None:
        rep = parse_formula_rows(GOOD.splitlines())
        assert all(not r.formula_id.startswith("---") for r in rep.rows)

    def test_backticks_are_stripped_from_values(self) -> None:
        row = parse_formula_rows(GOOD.splitlines()).rows[0]
        for v in (row.expression, row.dimension_text):
            assert v is not None and "`" not in v and "*" not in v


class TestRoleAssignmentIsByHeaderNotPosition:
    """列序不同必须照样抽对 —— 这是按角色取值而非按列位的意义。"""

    def test_reordered_columns(self) -> None:
        src = """\
| 公式 ID | 量纲 | 表达式 | 上游 |
|---|---|---|---|
| `F_J.2.1_BUCK` | `[V]` | `V_out = D · V_in` | A-2 |
"""
        row = parse_formula_rows(src.splitlines()).rows[0]
        assert row.expression == "V_out = D · V_in"
        assert row.dimension_text == "[V]"

    def test_minimal_two_column_table(self) -> None:
        src = """\
| 公式 ID | 表达式 |
|---|---|
| `F_E.1_OHM_LAW` | `U = I · R` |
"""
        row = parse_formula_rows(src.splitlines()).rows[0]
        assert row.expression == "U = I · R"
        assert row.dimension_text is None
        assert row.upstream == ()


class TestNoGuessing:
    """缺表头角色时整表不用, 不靠内容猜。"""

    def test_table_without_id_column_is_skipped_entirely(self) -> None:
        """表头没有「公式 ID」列 ⟹ 不产出, 且计入 unusable_tables。

        实测踩过的坑: 表头「公式与规则」含关键词「公式」, 会被判成 expression
        列, 于是公式 ID 自己被当成表达式。
        """
        src = """\
| 公式与规则 | 说明 |
|---|---|
| `F_M.2_DEBOUNCE`，**R13** | 防抖有效 |
"""
        rep = parse_formula_rows(src.splitlines())
        assert rep.rows == ()
        assert rep.unusable_tables == 1

    def test_expression_equal_to_the_id_is_rejected(self) -> None:
        """即使有 ID 列, 表达式格取到 ID 本身也必须挡掉。"""
        src = """\
| 名称 | 公式 ID | 表达式 |
|---|---|---|
| 抖动串抑制 | `F_M.2.3` | `F_M.2.3` |
"""
        rep = parse_formula_rows(src.splitlines())
        assert rep.rows == ()
        assert "表头角色误判" in rep.dropped[0][1]

    def test_id_column_without_expression_keeps_other_fields(self) -> None:
        """只有名称、没有表达式的行: **保留其字段**, 不整行丢弃。

        早先一版把这种行整行丢进 ``dropped``, 理由是「无表达式列」。但附录 U
        的公式登记表(公式 ID | 名称 | 上游 | 规则 | 测试)**根本没有表达式列**,
        于是附录 U 对抽取零贡献 —— 实测 83 条闭合公式因此缺 ``name_zh``, 而那些
        中文名在方案里明明写着。这是抽取器把登记表丢了, 不是方案缺口。
        """
        src = """\
| 名称 | 公式 ID | 说明 |
|---|---|---|
| 变位捕获 | `F_M.2.5` | SOE 条数 |
"""
        rep = parse_formula_rows(src.splitlines())
        assert [r.formula_id for r in rep.rows] == ["F_M.2.5"]
        assert rep.rows[0].name_zh == "变位捕获"
        assert rep.rows[0].expression is None, "没有表达式就是没有, 不许编"


class TestNonFormulaTablesIgnored:
    def test_table_mentioning_a_rule_not_a_formula_id(self) -> None:
        src = """\
| 约束 | 公式 | 规则 |
|---|---|---|
| 告警点先于保护点 | 普通量 | **R15** |
"""
        assert parse_formula_rows(src.splitlines()).rows == ()

    def test_prose_with_stray_equals_is_not_a_formula(self) -> None:
        src = """\
| 公式 ID | 名称 | 表达式 |
|---|---|---|
| `F_X.1_PROSE` | 开路电压 | = 端口开路电压， |
"""
        rep = parse_formula_rows(src.splitlines())
        # 表达式格被表头认定为 expression, 但值是中文散文 —— 保留行,
        # 由下游的表达式归一化/量纲门禁拒绝, 而不是抽取器猜。
        assert len(rep.rows) == 1
        assert rep.rows[0].expression == "= 端口开路电压，"[:]


class TestReport:
    def test_duplicate_ids_are_surfaced(self) -> None:
        src = """\
| 公式 ID | 表达式 |
|---|---|
| `F_E.1_OHM_LAW` | `U = I · R` |

| 公式 ID | 表达式 |
|---|---|
| `F_E.1_OHM_LAW` | `U = R · I` |
"""
        rep = parse_formula_rows(src.splitlines())
        assert rep.duplicate_ids == ("F_E.1_OHM_LAW",)

    def test_summary_reports_metadata_only_rows(self) -> None:
        """无表达式的行进 ``rows``, 所以 summary 不该再把它们算作「丢弃」。

        ``丢弃`` 现在只统计真正被挡掉的行(表达式列取到公式ID本身)。把
        「登记表行」继续算成丢弃, 会让人以为附录 U 的名称/上游被扔了。
        """
        src = """\
| 公式 ID | 名称 | 说明 |
|---|---|---|
| `F_M.2.5` | 变位捕获 | SOE 条数 |
"""
        rep = parse_formula_rows(src.splitlines())
        assert [r.formula_id for r in rep.rows] == ["F_M.2.5"]
        assert rep.rows[0].expression is None
        assert rep.dropped == ()
        assert "丢弃" in rep.summary()  # 全角冒号, 不硬编码整句

    def test_empty_shell_row_is_dropped_with_reason(self) -> None:
        """既无表达式又无任何字段的行是空壳, 丢掉并记明原因。

        否则「抽出 N 条」会被这类行虚高 —— 它们对下游毫无用处。
        """
        src = """\
| 公式 ID | 说明 |
|---|---|
| `F_M.2.5` | SOE 条数 |
"""
        rep = parse_formula_rows(src.splitlines())
        assert rep.rows == ()
        assert rep.dropped and "空壳行" in rep.dropped[0][1]


class TestFormulaIdPattern:
    @pytest.mark.parametrize(
        "s",
        [
            "F_J.2.1_BUCK",
            "F_M.3.4_CRC8_PEC",
            "F_E.1_OHM_LAW",
            "F_W.12.16_ADC_SNR",
            # 只到节号、无短名 —— 整节共用一条公式时语料就这么写
            "F_M.2.3",
            "F_L.5.4",
            "F_M.3.5",
            "F_L.4",
        ],
    )
    def test_accepts_real_ids(self, s: str) -> None:
        assert FORMULA_ID_RE.search(s)

    @pytest.mark.parametrize(
        "s",
        ["A-2", "R15", "F_J_BUCK", "F_M.2.3_lowercase", "G", ""],
    )
    def test_rejects_near_misses(self, s: str) -> None:
        assert not FORMULA_ID_RE.search(s), s


@pytest.mark.slow
def test_corpus_reaches_the_w1_floor() -> None:
    """真实语料必须够 §18.4.2 W1 验收门槛 (公式入库 ≥ 400 条)。

    这是唯一读方案全文的测试 —— 它是「抽取器没退化」的唯一证据, 而
    其它测试只验证解析逻辑, 语料变了它们不会红。
    """
    if not SPEC.is_file():
        pytest.skip(f"方案文件不在预期位置: {SPEC}")
    rep = parse_formula_rows(SPEC.read_text(encoding="utf-8").splitlines())
    distinct = {r.formula_id for r in rep.rows}
    assert len(distinct) >= 400, f"只抽出 {len(distinct)} 条, 低于 W1 门槛 400"
    # 有表达式的行必须是真表达式(绝不能是公式ID本身)。
    assert not [r for r in rep.rows if r.expression and FORMULA_ID_RE.search(r.expression)]
    # **无表达式的行合法**: 附录 U 的登记表只有名称/上游。但至少得带一个
    # 别的字段 —— 空壳行已在解析阶段丢掉, 这里兜底。
    orphans = [r for r in rep.rows
               if not r.expression and not (r.name_zh or r.upstream or r.dimension_text)]
    assert not orphans, f"空壳行: {[r.formula_id for r in orphans][:5]}"
    # 表达式不得是 ID 自身 (已在解析里挡, 这里复核一次)
    assert not [r for r in rep.rows if FORMULA_ID_RE.search(r.expression or "")]
