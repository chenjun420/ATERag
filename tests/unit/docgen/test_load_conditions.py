"""``docgen.load_conditions`` 的单元测试 —— 工况限定词的可计算性。

重点测三件事:

1. **比例基准是满载**, 不是绝对功率的百分数。``半载 = 0.5 × 满载`` 这条
   弄反了, 所有代入都会错, 而算式照样成立。
2. **额定/标称不给比例**。编一个比例值会让 Semantica 的规则在错误工况上运行,
   而且**不报错** —— 这是最危险的一类错。
3. **模式匹配必须整串锚定**。否则 ``P_load`` 里的 ``load`` 会被当成工况词。
"""

from __future__ import annotations

import pytest

from aterag.docgen.load_conditions import (
    BASE_LOAD,
    LOAD_CONDITIONS,
    QUALIFIER_KINDS,
    parse_percent_load,
    resolve,
)


class TestRatios:
    def test_full_load_is_the_base(self) -> None:
        """满载是基准, 比例恒为 1.0。"""
        assert BASE_LOAD == "满载"
        full = resolve("满载")
        assert full is not None
        assert full.ratio == 1.0

    def test_half_load_is_half_of_full_load(self) -> None:
        """半载 = 满载 × 50% —— 用户给定的定义。"""
        half = resolve("半载")
        full = resolve("满载")
        assert half is not None and full is not None
        assert half.ratio == 0.5
        assert half.ratio == full.ratio * 0.5

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("50%载", 0.5), ("30%载", 0.3), ("12.5%载", 0.125), ("100%载", 1.0)],
    )
    def test_percent_load(self, text: str, expected: float) -> None:
        assert (hit := parse_percent_load(text)) is not None
        assert hit.ratio == pytest.approx(expected)

    def test_percent_load_preserves_original_text(self) -> None:
        """``100%载`` 与「满载」等价, 但**不改写**原词。

        改写会丢方案原文, 而原文是审计依据 —— 「哪个词是我方加的」必须可查。
        """
        hit = parse_percent_load("100%载")
        assert hit is not None
        assert hit.zh == "100%载"
        assert hit.ratio == 1.0

    def test_no_load_is_zero(self) -> None:
        assert (hit := resolve("空载")) is not None
        assert hit.ratio == 0.0


class TestNonRatioQualifiers:
    @pytest.mark.parametrize("word", ["额定", "标称", "最大额定"])
    def test_rating_words_have_no_ratio(self, word: str) -> None:
        """额定/标称**没有**比例。

        给出比例会让规则在错误工况上运行且不报错 —— 比不给更危险, 因为
        不给会显式失败。
        """
        hit = resolve(word)
        assert hit is not None
        assert hit.ratio is None, f"{word} 不该有比例值"
        assert hit.kind in ("rating", "nominal")

    def test_minimum_load_has_no_ratio(self) -> None:
        """「最小载」收录但不给比例。

        最小稳定负载由具体拓扑决定, 不是常数。方案里出现 0 次, 无从核实。
        """
        hit = resolve("最小载")
        assert hit is not None
        assert hit.ratio is None
        assert hit.kind == "load"

    def test_rated_and_nominal_are_different_kinds(self) -> None:
        """额定 与 标称 **不是同一个概念**, 不能合成一条。

        GB/IEC 体系里额定是保证值, 标称是代表值。方案里 `COND_VIN_NOM` 的 ID
        是 NOM 而中文写「额定」, 正说明两者被混用过。
        """
        assert resolve("额定").kind != resolve("标称").kind  # type: ignore[union-attr]


class TestAnchoring:
    @pytest.mark.parametrize(
        "text",
        ["P_load", "load", "负载", "30%", "载30", "rated_load", "半载率"],
    )
    def test_lookalikes_are_not_conditions(self, text: str) -> None:
        """模式必须**整串锚定**。

        ``P_load`` 里的 ``load`` 是符号的一部分, 不是工况词。不锚定就会把它
        认成「满载」, 于是所有带 ``_load`` 后缀的符号都凭空获得比例 1.0。
        """
        assert parse_percent_load(text) is None
        assert resolve(text) is None or resolve(text).zh not in ("满载", "半载", "空载")

    def test_out_of_range_percent_rejected(self) -> None:
        """``500%载`` 拒收 —— 不是载类工况, 是数据错误。不猜。"""
        assert parse_percent_load("500%载") is None
        assert parse_percent_load("-10%载") is None


class TestProvenance:
    def test_every_entry_has_a_note(self) -> None:
        """每条都要有依据 —— 没有依据的候选不收。"""
        for item in (*LOAD_CONDITIONS, *QUALIFIER_KINDS):
            assert item.note.strip(), f"{item.zh} 缺依据"

    def test_every_entry_has_english(self) -> None:
        """中英对照是给 Semantica 做检索用的, 不能只留中文。"""
        for item in (*LOAD_CONDITIONS, *QUALIFIER_KINDS):
            assert item.en.strip(), f"{item.zh} 缺英文名"
