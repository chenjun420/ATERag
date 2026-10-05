"""``_split_refs`` 的记号解析 —— 钉住「不带空格切掉说明」这条规则。

这个函数改坏过两次, 两次都是**静默**的: ``fullmatch`` 失败时它只是不收这个
记号, 生成器不报错、测试也可能照过, 而公理 ``formula_refs`` 从 12 条掉到 1 条。
所以这里逐个钉住短记号形态。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "_build_seed", _ROOT / "scripts" / "build_seed_data.py"
)
assert _spec and _spec.loader
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


class TestTokenShape:
    @pytest.mark.parametrize(
        "token",
        [
            # 单段。**R5 / P3 的数字段是单字符** —— 早期正则写成
            # `[AFGRPETUW]([.-][A-Za-z0-9_]+)?` 时首段只吃一个字符,
            # `R5` 的 `5` 之后没有分隔符可吃, 整体不匹配。
            "R5",
            "P3",
            "A-1",
            "A-J.1",
            "T10",
            "G.26",
            "E",
            # 多段 + 下划线后缀。这是方案索引表里的主流形态。
            "F_E.1",
            "F_E.1_OHM_LAW",
            "F_J.3_INDUCTOR_RIPPLE",
            "F_M.2_RIPPLE_RMS_CONV",
            # 首段不止一个字符: 记号可以以多个同类字符开头。
            "FF_gain",
            "TT_load",
        ],
    )
    def test_recognised(self, token: str) -> None:
        assert build._looks_like_token(token), f"合法记号被拒: {token}"

    @pytest.mark.parametrize(
        "text",
        [
            "欧姆定律",
            "瞬态恢复时间",
            # 说明跟在记号后面: fullmatch 必须失败, 否则说明会被吞进记号。
            "F_E.1_OHM_LAW 欧姆定律",
            "R5 通例",
            "F_J.*",
            "",
        ],
    )
    def test_rejected(self, text: str) -> None:
        assert not build._looks_like_token(text), f"非记号被收: {text!r}"


class TestSplitRefs:
    def test_strips_trailing_prose(self) -> None:
        """``R5 通例`` -> ``R5``。留着说明会让关系 target 永远匹配不上。"""
        assert build._split_refs("R5 通例") == ["R5"]

    def test_keeps_underscore_suffix(self) -> None:
        """``F_E.1_OHM_LAW 欧姆定律`` -> ``F_E.1_OHM_LAW``, 不是 ``F_E.1``。

        这条是回归: 早先无条件 ``split(" ")[0]``, 把索引表里写全的
        ``F_E.1_OHM_LAW`` 砍成 ``F_E.1``, 而库里 id 就是 ``F_E.1_OHM_LAW``
        —— 于是这条引用永远匹配不上, 且没有任何报错。
        """
        assert build._split_refs("F_E.1_OHM_LAW 欧姆定律") == ["F_E.1_OHM_LAW"]

    def test_multiple_separators(self) -> None:
        """逗号/分号/顿号都算分隔。顿号是中文表格里的常见写法。"""
        assert build._split_refs("R1, R5; R10、P3") == ["R1", "R5", "R10", "P3"]

    def test_drops_prose_only_cell(self) -> None:
        """整格都是说明时返回空, 而不是把说明当记号收进来。"""
        assert build._split_refs("见下方展开") == []

    def test_backtick_free_text_from_spec(self) -> None:
        """方案 md 里公式格是反引号包裹的, ``_clean`` 已去掉反引号。"""
        assert build._split_refs("`F_E.1` 欧姆定律 + 功率闭合") == ["F_E.1"]


class TestPruneCleansDanglingRefs:
    def test_dropped_formula_is_removed_from_axiom_refs(self) -> None:
        """剪掉公式后, 指向它的 ``formula_refs`` 也要跟着摘掉。

        不摘的话公理上留着一个指向空处的引用 —— 追溯链「看起来连通、实际指向空」,
        而下游无法区分「这条公理没有公式支撑」和「公式 id 写错了」。
        """
        ents = [
            {
                "id": "F_J.9_DESIGN_SIDE",
                "name": "x",
                "type": "formula",
                "properties": {"domain": "J"},
            },
            {
                "id": "F_E.1_OHM_LAW",
                "name": "y",
                "type": "formula",
                "properties": {"domain": "E"},
            },
            {
                "id": "A-1",
                "name": "KCL",
                "type": "axiom",
                "properties": {"formula_refs": ["F_J.9_DESIGN_SIDE", "F_E.1_OHM_LAW"]},
            },
        ]
        kept, _rels, stats = build.prune_non_executable(ents, [])
        axiom = next(e for e in kept if e["id"] == "A-1")
        assert stats["formula"] == 1
        assert axiom["properties"]["formula_refs"] == ["F_E.1_OHM_LAW"]
