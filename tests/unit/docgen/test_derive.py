"""``docgen.derive`` —— D2 的 ``derive_from`` 补全策略。

核心不变量: **每一次补全都必须带来源标记**, 且「继承」与「声明」在数据上可
区分。否则审计时无法分辨某个出处是方案写的、还是我们补的 —— 而这正是本项目
一路在防的失效模式(静默地拿到一个看似合理的值)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aterag.docgen.derive import (
    EMPIRICAL_PENDING,
    derive_all,
    section_of,
    summarize,
)
from aterag.docgen.spec_parse import FormulaRow, parse_formula_rows, read_spec

SPEC = Path(__file__).resolve().parents[3] / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md"


def row(fid: str, upstream: tuple[str, ...] = ()) -> FormulaRow:
    return FormulaRow(formula_id=fid, upstream=upstream)


class TestSectionOf:
    @pytest.mark.parametrize(
        ("fid", "expect"),
        [
            ("F_J.2.4_CUK", "F_J.2"),
            ("F_J.2", "F_J.2"),
            ("F_N.4.4_PART_STRESS_MODEL", "F_N.4"),
            ("F_W.7.10_RDS_ON_TEMP", "F_W.7"),
        ],
    )
    def test_章是域字母加第一个数字段(self, fid: str, expect: str) -> None:
        assert section_of(fid) == expect

    def test_不匹配时退化为原ID(self) -> None:
        """退化的意义: 该公式只跟自己一组, 绝不会继承到别人的出处。"""
        assert section_of("WEIRD") == "WEIRD"


class TestDeriveAll:
    def test_方案明写的原样透传(self) -> None:
        """``explicit`` 不许被改写 —— 哪怕章内兄弟取值不同。"""
        rows = {"F_J.2.1_BUCK": row("F_J.2.1_BUCK", ("A-2", "A-4", "T3"))}
        d = derive_all(rows)["F_J.2.1_BUCK"]
        assert d.is_explicit
        assert d.refs == ("A-2", "A-4", "T3")
        assert not d.review_required

    def test_章内取值一致才继承(self) -> None:
        rows = {
            "F_J.2.1_BUCK": row("F_J.2.1_BUCK", ("A-2", "A-4", "T3")),
            "F_J.2.4_CUK": row("F_J.2.4_CUK"),
        }
        d = derive_all(rows)["F_J.2.4_CUK"]
        assert d.is_inherited
        assert d.provenance == "inherited:F_J.2"
        assert d.refs == ("A-2", "A-4", "T3")
        assert not d.review_required

    def test_章内取值不一致时不猜(self) -> None:
        """方案在该章自相矛盾 -> 任何选取都是猜测, 故**不继承**。

        这是最重要的一条: 语料实测 31 个章的出处取值不一致(``F_J.5`` 有 3 种),
        若「取第一个」或「取多数」就会**静默**写入一个可能错误的溯源。
        """
        rows = {
            "F_J.5.1_A": row("F_J.5.1_A", ("A-10", "T12")),
            "F_J.5.2_B": row("F_J.5.2_B", ("A-11",)),
            "F_J.5.3_C": row("F_J.5.3_C"),
        }
        d = derive_all(rows)["F_J.5.3_C"]
        assert not d.is_inherited
        assert d.refs == (EMPIRICAL_PENDING,)
        assert d.review_required

    def test_章内无任何出处时标待复核(self) -> None:
        rows = {"F_J.8.1_SHOOT_THROUGH": row("F_J.8.1_SHOOT_THROUGH")}
        d = derive_all(rows)["F_J.8.1_SHOOT_THROUGH"]
        assert d.provenance == "empirical-pending-review"
        assert d.review_required
        assert "待复核" in d.refs[0]

    def test_跨章不继承(self) -> None:
        """``F_J.2`` 的出处不得被 ``F_J.5`` 的公式继承。"""
        rows = {
            "F_J.2.1_BUCK": row("F_J.2.1_BUCK", ("A-2",)),
            "F_J.5.1_A": row("F_J.5.1_A"),
        }
        assert derive_all(rows)["F_J.5.1_A"].review_required

    def test_未闭合的兄弟也能提供出处(self) -> None:
        """``upstream`` 是元数据, 与量纲闭合无关, 未闭合的兄弟照样可用。"""
        rows = {
            "F_J.2.1_BUCK": row("F_J.2.1_BUCK"),  # 无表达式, 相当于未闭合
            "F_J.2.9_UNKNOWN": row("F_J.2.9_UNKNOWN", ("A-2",)),
            "F_J.2.4_CUK": row("F_J.2.4_CUK"),
        }
        assert derive_all(rows)["F_J.2.4_CUK"].refs == ("A-2",)

    def test_每个公式都有来源标记(self) -> None:
        rows = {"F_A.1_X": row("F_A.1_X"), "F_B.1_Y": row("F_B.1_Y", ("A-1",))}
        assert all(d.provenance for d in derive_all(rows).values())


class TestCorpusDerive:
    """语料级: 补全后不得有任何公式缺 ``derive_from``(G4 判据)。"""

    def test_语料全覆盖且缺口具名(self) -> None:
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        first: dict[str, FormulaRow] = {}
        for r in parse_formula_rows(read_spec(SPEC)).rows:
            first.setdefault(r.formula_id, r)
        derivations = derive_all(first)
        assert set(derivations) == set(first), "有公式没拿到 derive_from"
        # 待复核那批必须**可枚举**: 缺口要能点名, 不能只是个计数。
        pending = [f for f, d in derivations.items() if d.review_required]
        assert len(pending) > 0
        by_prov = summarize(derivations)
        assert sum(by_prov.values()) == len(first)
        assert by_prov["explicit"] > 0
