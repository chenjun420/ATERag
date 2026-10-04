"""``docgen.errata`` 的单元测试 —— 勘误 E-1~E-7 与公理索引。

W1 判据: 「勘误公式 7/7 带 errata」。这里既测抽到了 7 条, 也测**哪条靠什么
关联上的** —— 因为 3 条靠的是声明而不是抽取, 不标出来就分不清哪些结论有文档
支撑、哪些是我们补的。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from aterag.docgen.errata import (
    CSV_COLUMNS,
    DECLARED_ERRATA_LINKS,
    parse_axiom_index,
    parse_errata,
    reverse_index,
    write_errata_csv,
)
from aterag.docgen.spec_parse import read_spec

SPEC = Path(__file__).resolve().parents[3] / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md"

SNIPPET = """\
## 附录I 公理

| 公理 | 定理 | 公式（部分） | 规则 | 测试 |
|---|---|---|---|---|
| A-1 KCL | T1 Tellegen | `F_E.1` 欧姆定律 | R5 | G.26 |
| A-4 电感伏秒 | T3 伏秒平衡 | `F_J.3_INDUCTOR_RIPPLE`, `F_J.4_OUTPUT_RIPPLE` | R2 | G.3 |
| A-5 电容电荷 | T3 安秒平衡 | `F_J.4_OUTPUT_RIPPLE` | R8 | G.3 |
| A-11 量纲齐次 | — | 全部公式的量纲向量 | R1 | 全部 |

> **勘误 E-1**：常见误表述：把 KVL 标注为「能量守恒：ΣV = 0」。KVL 是 A-2；
> 能量守恒是 A-1 的推论。

> **勘误 E-2**：常见误表述「电感中电流不能突变……类似于电流源」。
> 严格表述必须带 `|v_L| < ∞` 这个前提。

> **勘误 E-5**：易混淆的写法，零点被计入两次。

| `F_K.4.2_TYPE_II` | 补偿器 | 勘误 E-5 |
"""


class TestErrataExtraction:
    def test_finds_all_tags(self) -> None:
        rep = parse_errata(SNIPPET.splitlines())
        assert [e.tag for e in rep.errata] == ["E-1", "E-2", "E-5"]

    def test_blockquote_continuation_is_captured(self) -> None:
        """勘误常写成多行引用块, 公理落点常在第二行。

        只读首行会让 E-2 一个公理都抽不到 —— 实测踩过。
        """
        rep = parse_errata(SNIPPET.splitlines())
        assert "前提" in next(e for e in rep.errata if e.tag == "E-2").text

    def test_axiom_ids_survive_cjk_adjacency(self) -> None:
        """``A-4 电感伏秒`` 里 ``4`` 与 ``电`` 之间**没有**词边界。

        Python 的 ``\\w`` 含 CJK, 所以 ``A-4\\b`` 匹配不上 —— 实测导致 E-2/E-3
        一个公理都没抽到。
        """
        rep = parse_errata(SNIPPET.splitlines())
        assert "A-2" in next(e for e in rep.errata if e.tag == "E-1").axioms

    def test_direct_mention_links_formulas(self) -> None:
        """E-5 的修正对象写在别处的登记表里, 不在勘误正文里。"""
        rep = parse_errata(SNIPPET.splitlines())
        e5 = next(e for e in rep.errata if e.tag == "E-5")
        assert "F_K.4.2_TYPE_II" in e5.formula_refs
        assert "正文点名" in e5.link_note

    def test_two_hop_link_through_axiom_index(self) -> None:
        rep = parse_errata(SNIPPET.splitlines())
        e1 = next(e for e in rep.errata if e.tag == "E-1")
        assert "F_E.1" in e1.formula_refs
        assert e1.link_note.startswith("经 ")

    def test_declared_links_are_labelled_as_declarations(self) -> None:
        """声明式落点必须标出来, 否则分不清哪些结论有文档支撑。"""
        rep = parse_errata(SNIPPET.splitlines())
        e2 = next(e for e in rep.errata if e.tag == "E-2")
        assert "声明" in e2.link_note
        assert "F_J.3_INDUCTOR_RIPPLE" in e2.formula_refs

    def test_unlinked_is_reported_not_hidden(self) -> None:
        """未关联的勘误必须显式说明, 空列表不能冒充「已关联」。"""
        src = SNIPPET.replace("| A-4 电感伏秒 | T3 伏秒平衡 | `F_J.3_INDUCTOR_RIPPLE`, `F_J.4_OUTPUT_RIPPLE` | R2 | G.3 |", "")
        src = src.replace("DECLARED", "")
        rep = parse_errata(src.splitlines())
        e2 = next(e for e in rep.errata if e.tag == "E-2")
        assert e2.formula_refs == ()
        assert "未关联" in e2.link_note


class TestAxiomIndex:
    def test_reads_rules_and_tests(self) -> None:
        rows, _ = parse_axiom_index(SNIPPET.splitlines())
        by_id = {r.axiom_id: r for r in rows}
        assert by_id["A-4"].rules == ("R2",)
        assert by_id["A-4"].tests == ("G.3",)

    def test_multi_formula_cell_is_split(self) -> None:
        rows, _ = parse_axiom_index(SNIPPET.splitlines())
        a4 = next(r for r in rows if r.axiom_id == "A-4")
        assert a4.formula_refs == ("F_J.3_INDUCTOR_RIPPLE", "F_J.4_OUTPUT_RIPPLE")

    def test_rows_without_formula_ids_are_dropped_with_reason(self) -> None:
        """``全部公式的量纲向量`` 这种不是公式 ID。"""
        rows, dropped = parse_axiom_index(SNIPPET.splitlines())
        assert all(r.axiom_id != "A-11" for r in rows)
        assert any("公式列" in reason for _ln, reason, _f in dropped)


class TestReverseIndex:
    def test_rule_and_test_keys_present(self) -> None:
        rep = parse_errata(SNIPPET.splitlines())
        idx = reverse_index(rep)
        assert "R2" in idx["by_rule"]
        assert "G.3" in idx["by_test"]

    def test_refs_are_deduplicated(self) -> None:
        """F_J.4_OUTPUT_RIPPLE 同时来自 A-4 与 A-5, 索引里只能出现一次。"""
        rep = parse_errata(SNIPPET.splitlines())
        idx = reverse_index(rep)
        g3 = idx["by_test"]["G.3"]
        assert len(g3) == len(set(g3))
        assert "F_J.4_OUTPUT_RIPPLE" in g3


class TestCsv:
    def test_round_trips(self, tmp_path: Path) -> None:
        rep = parse_errata(SNIPPET.splitlines())
        out = write_errata_csv(rep.errata, tmp_path / "errata.csv")
        with out.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert tuple(rows[0]) == CSV_COLUMNS
        assert len(rows) == len(rep.errata)


@pytest.mark.slow
class TestCorpus:
    def test_all_seven_found_and_linked(self) -> None:
        """W1 判据: 勘误公式 7/7 带 errata。"""
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        rep = parse_errata(read_spec(SPEC))
        assert len(rep.errata) == 7, rep.summary()
        assert len(rep.linked) == 7, rep.summary()

    def test_tags_are_exactly_e1_to_e7(self) -> None:
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        rep = parse_errata(read_spec(SPEC))
        assert [e.tag for e in rep.errata] == [f"E-{k}" for k in range(1, 8)]

    def test_declared_links_all_resolve(self) -> None:
        """声明的公理必须在公理索引里真有对应公式, 否则声明是空的。"""
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        rep = parse_errata(read_spec(SPEC))
        for tag in DECLARED_ERRATA_LINKS:
            err = next(e for e in rep.errata if e.tag == tag)
            assert err.formula_refs, f"{tag} 的声明落点解析不出公式"

    def test_axiom_index_is_substantial(self) -> None:
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        rep = parse_errata(read_spec(SPEC))
        assert len(rep.axiom_rows) >= 50, f"公理索引只有 {len(rep.axiom_rows)} 行"
        idx = reverse_index(rep)
        assert len(idx["by_rule"]) >= 5
        assert len(idx["by_test"]) >= 5
