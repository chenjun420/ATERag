"""``docgen.standards`` 的单元测试 —— 附录 V -> ``standards_registry``。

W1 验收判据之一是「标准索引 ≥ 60 条」。这里既测抽取, 也测**不抽错**:
引用状态、编号识别、合并规则各有专门的用例, 因为这三样都会安静地出错。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from aterag.docgen.spec_parse import read_spec
from aterag.docgen.standards import (
    CSV_COLUMNS,
    parse_standards,
    write_standards_csv,
)

SPEC = Path(__file__).resolve().parents[3] / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md"

SNIPPET = """\
## 附录V 引用与知识来源参考

### V.0 本章定位

不是引用规范。

### V.1 安全规范（安规）

| 标准号 | 年份 | 名称 | 适用范围 | 关联规则/测试 |
|---|---|---|---|---|
| **IEC 60664-1** | 2020 | 低压系统内设备的绝缘配合 | 电气间隙、爬电距离 | R10、**G.12**、E.9 |
| IEC 60664-2-1 | — | 采用基础绝缘的间隙 | 间隔计算 | G.12 |
| IEC 60529 | 2013 | 外壳防护等级 | IP 代码 | F_L.8.3 |

### V.2 电磁兼容（EMC）

| 标准号 | 年份 | 名称 | 适用范围 | 关联规则/测试 |
|---|---|---|---|---|
| IEC 60529 | 1989 | 旧版 IP | 老设备 | G.12 |
| PMBus Specification Part I | 1.3.1 | 一般要求 | 传输与电气 | F_M.3.1 |

### V.10 公式出处映射

| 公式 | 来源 | 说明 |
|---|---|---|
| F_J.2.1 | 某处 | 不是标准表 |
"""


class TestExtraction:
    def test_reads_every_row(self) -> None:
        rep = parse_standards(SNIPPET.splitlines())
        assert [r.standard_id for r in rep.records][:2] == ["IEC 60664-1", "IEC 60664-2-1"]

    def test_strips_markdown_emphasis(self) -> None:
        rep = parse_standards(SNIPPET.splitlines())
        assert rep.records[0].standard_id == "IEC 60664-1"

    def test_version_dash_becomes_none(self) -> None:
        """``—`` 是「原文没写年份」, 不是「版本为零」。"""
        rep = parse_standards(SNIPPET.splitlines())
        assert rep.records[0].version == "2020"
        assert rep.records[1].version is None

    def test_bindings_are_extracted(self) -> None:
        rep = parse_standards(SNIPPET.splitlines())
        assert rep.records[0].bindings == ("R10", "G.12", "E.9")

    def test_formula_refs_are_extracted(self) -> None:
        rep = parse_standards(SNIPPET.splitlines())
        iec60529 = next(r for r in rep.records if r.standard_id == "IEC 60529")
        assert "F_L.8.3" in iec60529.formula_refs

    def test_section_scope_is_recorded(self) -> None:
        rep = parse_standards(SNIPPET.splitlines())
        assert rep.records[0].domain.startswith("V.1")
        pmbus = next(r for r in rep.records if r.standard_id.startswith("PMBus"))
        assert pmbus.domain.startswith("V.2")

    def test_non_standard_sections_are_not_read(self) -> None:
        """V.10「公式出处映射」的表长得像标准表, 但它不是。"""
        rep = parse_standards(SNIPPET.splitlines())
        assert all("V.10" not in r.domain for r in rep.records)
        assert all(r.standard_id != "F_J.2.1" for r in rep.records)


class TestNameOnlyStandards:
    def test_specification_without_a_number_is_kept(self) -> None:
        """PMBus / CAN FD / IRIG-B 是真规范, 只是没有编号。

        要求首列含数字会把它们判成散文 —— 实测这样丢掉 47 条真规范。
        """
        rep = parse_standards(SNIPPET.splitlines())
        pmbus = next(r for r in rep.records if r.standard_id.startswith("PMBus"))
        assert pmbus.title == "一般要求"

    def test_has_number_distinguishes_the_two_kinds(self) -> None:
        """「是真标准但没编号」与「不是标准」必须能区分。"""
        rep = parse_standards(SNIPPET.splitlines())
        by_id = {r.standard_id: r for r in rep.records}
        assert by_id["IEC 60664-1"].has_number is True
        assert by_id["PMBus Specification Part I"].has_number is False

    def test_compound_ids_are_not_split(self) -> None:
        """``GB 9706.1 / GB/T 16935`` 一格两个标准。

        硬拆会造出 ``2``、``-6`` 这种凭空捏造的标准号, 而 registry 的键必须
        能追溯回原文。
        """
        src = SNIPPET.replace(
            "| IEC 60664-2-1 | — | 采用基础绝缘的间隙 | 间隔计算 | G.12 |",
            "| GB 9706.1 / GB/T 16935 | — | 绝缘配合 | 间隙/爬电 | E.9 |",
        )
        rep = parse_standards(src.splitlines())
        ids = [r.standard_id for r in rep.records]
        assert "GB 9706.1 / GB/T 16935" in ids
        assert "GB/T 16935" not in ids


class TestMerge:
    def test_same_standard_in_two_sections_merges(self) -> None:
        """``IEC 60529`` 在 V.1 与 V.2 各出现一次, 而 registry 以标准号为键。"""
        rep = parse_standards(SNIPPET.splitlines())
        ids = [r.standard_id for r in rep.records]
        assert ids.count("IEC 60529") == 1

    def test_merge_unions_domains_and_bindings(self) -> None:
        rep = parse_standards(SNIPPET.splitlines())
        merged = next(r for r in rep.records if r.standard_id == "IEC 60529")
        assert "V.1" in merged.domain and "V.2" in merged.domain
        assert "G.12" in merged.bindings and "F_L.8.3" in merged.formula_refs

    def test_merge_keeps_a_known_version(self) -> None:
        """``—`` 不该覆盖掉一个真版本 —— 那是在丢信息。"""
        src = SNIPPET.replace(
            "| IEC 60529 | 2013 | 外壳防护等级 | IP 代码 | F_L.8.3 |",
            "| IEC 60529 | — | 外壳防护等级 | IP 代码 | F_L.8.3 |",
        )
        rep = parse_standards(src.splitlines())
        merged = next(r for r in rep.records if r.standard_id == "IEC 60529")
        assert merged.version == "1989"


class TestCitationStatus:
    def test_everything_is_unverified(self) -> None:
        """V.0 第四条规定: 未经标准库核验的一律 UNVERIFIED。

        本模块只从文档抽文本, 没连任何标准库 —— 把「已核验」当默认值等于
        让下游以为我们核对过, 而「文档里写了这个号」与「这个号真实存在且
        版本正确」是两件事, 前者我们能保证, 后者不能。
        """
        rep = parse_standards(SNIPPET.splitlines())
        assert all(r.citation_status == "UNVERIFIED" for r in rep.records)


class TestCsv:
    def test_round_trips(self, tmp_path: Path) -> None:
        rep = parse_standards(SNIPPET.splitlines())
        out = write_standards_csv(rep.records, tmp_path / "standards.csv")
        with out.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == len(rep.records)
        assert tuple(rows[0]) == CSV_COLUMNS

    def test_list_fields_use_pipe(self, tmp_path: Path) -> None:
        """用 ``|`` 而不是逗号连接, 免得 CSV 里再套一层引号。"""
        rep = parse_standards(SNIPPET.splitlines())
        out = write_standards_csv(rep.records, tmp_path / "standards.csv")
        with out.open(encoding="utf-8", newline="") as fh:
            row = next(csv.DictReader(fh))
        assert row["bindings"] == "R10|G.12|E.9"

    def test_column_order_is_explicit(self) -> None:
        """列序是 seed 文件的接口, 不能靠 dict 插入序。"""
        assert CSV_COLUMNS[0] == "standard_id"
        assert "citation_status" in CSV_COLUMNS


@pytest.mark.slow
class TestCorpus:
    def test_meets_the_w1_floor(self) -> None:
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        rep = parse_standards(read_spec(SPEC))
        assert len(rep.records) >= 60, rep.summary()

    def test_no_rows_dropped_in_corpus(self) -> None:
        """丢掉任何一行都是丢标准 —— 语料里 V.1~V.9 全是标准表。"""
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        rep = parse_standards(read_spec(SPEC))
        assert not rep.dropped, rep.summary()

    def test_all_nine_sections_present(self) -> None:
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        rep = parse_standards(read_spec(SPEC))
        assert len(rep.sections_seen) == 9, rep.summary()

    def test_no_duplicate_keys(self) -> None:
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        rep = parse_standards(read_spec(SPEC))
        ids = [r.standard_id for r in rep.records]
        assert len(ids) == len(set(ids)), "registry 以标准号为键, 撞键就是数据缺陷"

    def test_most_entries_carry_bindings(self) -> None:
        """没有关联记号的条目进不了 by_rule 反向索引, 覆盖率会掉。"""
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        rep = parse_standards(read_spec(SPEC))
        assert rep.with_bindings >= 0.5 * len(rep.records), rep.summary()
