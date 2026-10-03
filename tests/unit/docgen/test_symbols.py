"""``docgen.symbols`` 的单元测试 —— U.5 符号表与量纲记号解析。

本模块的价值全在「不把错量纲静默算成对量纲」, 所以测试重心是**逐条核对
量纲记号**, 而不是行数与形状。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aterag.docgen.symbols import (
    parse_spec_dimension,
    parse_symbol_table,
)
from aterag.solver.symbolic import DIMENSIONLESS, Dimension

SPEC = Path(__file__).resolve().parents[3] / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md"

LEN, MAS, TIM, CUR, TMP, AMT, LUM = (
    "length",
    "mass",
    "time",
    "current",
    "temperature",
    "amount",
    "luminous_intensity",
)


def d(**kw: float) -> Dimension:
    base = dict.fromkeys((LEN, MAS, TIM, CUR, TMP, AMT, LUM), 0.0)
    base.update(kw)
    return Dimension(**base)  # type: ignore[arg-type]


class TestSpecDimensionNotation:
    """U.5 的方括号记号不能丢给 pint —— 见模块 docstring 的特斯拉陷阱。"""

    @pytest.mark.parametrize(
        ("text", "want"),
        [
            ("[V]", d(**{LEN: 2, MAS: 1, TIM: -3, CUR: -1})),
            ("[A]", d(**{CUR: 1})),
            ("[K]", d(**{TMP: 1})),
            ("[Hz]", d(**{TIM: -1})),
            ("[Ω]", d(**{LEN: 2, MAS: 1, TIM: -3, CUR: -2})),
            ("[W]", d(**{LEN: 2, MAS: 1, TIM: -3})),
            ("[J]", d(**{LEN: 2, MAS: 1, TIM: -2})),
            ("[H]", d(**{LEN: 2, MAS: 1, TIM: -2, CUR: -2})),
            ("[F]", d(**{LEN: -2, MAS: -1, TIM: 4, CUR: 2})),
        ],
    )
    def test_derived_units(self, text: str, want: Dimension) -> None:
        assert parse_spec_dimension(text) == want

    def test_division_is_not_multiplication(self) -> None:
        """``K/W`` 是温度/功率。忽略除号会读成 ``K·W`` —— 全错且不报错。"""
        assert parse_spec_dimension("[K/W]") == d(**{LEN: -2, MAS: -1, TIM: 3, TMP: 1})

    def test_bracketed_division(self) -> None:
        """``[A/Wb]`` 里的分母是带括号的一段。"""
        assert parse_spec_dimension("[A·turn/Wb]") == d(**{LEN: -2, MAS: -1, TIM: 2, CUR: 2})

    def test_superscript_digits_are_folded(self) -> None:
        """``A²`` 的 ² 是 U+00B2, ``T⁻¹`` 的 ⁻¹ 是 U+207B+U+00B9。

        它们不是 ``[0-9]``。不折算则指数整段匹配不上 —— ``A²`` 被读成 ``A``,
        量纲错一整个幂次。
        """
        assert parse_spec_dimension("[A²·s]") == d(**{CUR: 2, TIM: 1})
        assert parse_spec_dimension("[T⁻¹]") == d(**{TIM: -1})

    def test_t_is_time_not_tesla(self) -> None:
        """U.5 的 ``[T]`` 是时间。``unit_dimension("T")`` 返回特斯拉 —— 不报错, 全错。"""
        assert parse_spec_dimension("[T]") == d(**{TIM: 1})
        assert parse_spec_dimension("[T]") != d(**{MAS: 1, TIM: -2, CUR: -1})

    def test_ev_has_energy_dimension(self) -> None:
        assert parse_spec_dimension("[eV]") == parse_spec_dimension("[J]")

    def test_dimensionless_tokens(self) -> None:
        for t in ("无量纲", "1", "—", "[°]", "[dB]", "[bit]", "[rad/s]"):
            assert parse_spec_dimension(t) is not None, t
        assert parse_spec_dimension("无量纲") == DIMENSIONLESS
        assert parse_spec_dimension("[rad/s]") == d(**{TIM: -1})

    @pytest.mark.parametrize("text", ["见各条", "视状态而定", "见附录 F", "", None])
    def test_undefined_returns_none(self, text: str | None) -> None:
        """「本表未给」必须是 None 而不是猜一个 —— §18.10 注 2 的纪律。"""
        assert parse_spec_dimension(text) is None

    def test_unknown_unit_returns_none(self) -> None:
        assert parse_spec_dimension("[furlong]") is None


U5 = """\
### U.5 全局符号表（命名空间隔离）

| 裸符号 | 电气含义 | 热学含义 |
|---|---|---|
| `R` | 电阻 | — |

**统一符号表（正式）**：

| 符号 | 含义 | 量纲 | 命名空间 |
|---|---|---|---|
| `V_in`, `V_out`, `V_ref` | 电压 | `[V]` | `E` |
| `D`（占空比） | 占空比 | 无量纲 | `J` |
| `k`(下垂系数), `I_avg` | 下垂斜率/平均电流 | `[Ω]`,`[A]` | `Q` |
| `DC`(诊断覆盖率), `PFH` | 功能安全指标 | 见各条 | `L5` |
| `U`, `u_c`, `u_A`, `u_B`, `k` | 不确定度、包含因子 | `[X]`,`[1]` | `M` |

### U.6 客户端边界问题
"""


class TestSymbolTableParsing:
    @pytest.fixture(scope="class")
    def parsed(self) -> tuple[object, object]:
        return parse_symbol_table(U5.splitlines())

    def test_skips_the_ambiguity_table(self, parsed: tuple[object, object]) -> None:
        """第一张表 (裸符号歧义说明) 不是定义表, 不得混入。"""
        tbl, _ = parsed
        assert "R" not in tbl.by_symbol  # type: ignore[attr-defined]

    def test_comma_separated_symbols_share_one_dimension(self, parsed: tuple[object, object]) -> None:
        tbl, _ = parsed
        want = parse_spec_dimension("[V]")
        for s in ("V_in", "V_out", "V_ref"):
            assert tbl.resolve_dimension(s, "E") == want  # type: ignore[attr-defined]

    def test_backtick_annotation_is_stripped(self, parsed: tuple[object, object]) -> None:
        """``D`（占空比）`` 必须变成 ``D``。

        早先一版先剥反引号再删括号, 留下 ``D``` —— 字典里凭空多一个带反引号
        的符号, 而 ``D`` 本体查不到。
        """
        tbl, _ = parsed
        assert tbl.resolve_dimension("D", "J") == DIMENSIONLESS  # type: ignore[attr-defined]

    def test_positional_mapping_when_counts_match(self, parsed: tuple[object, object]) -> None:
        tbl, _ = parsed
        assert tbl.resolve_dimension("k", "Q") == parse_spec_dimension("[Ω]")  # type: ignore[attr-defined]
        assert tbl.resolve_dimension("I_avg", "Q") == parse_spec_dimension("[A]")  # type: ignore[attr-defined]

    def test_mismatched_counts_are_not_guessed(self, parsed: tuple[object, object]) -> None:
        """5 个符号 2 个量纲时按位对应会把 ``k``(包含因子) 判成 ``[X]``。

        方案没给可机械判定的对应关系 ⟹ 不猜, 记 ambiguous 并让 dimension=None。
        """
        tbl, rep = parsed
        assert tbl.resolve_dimension("k", "M") is None  # type: ignore[attr-defined]
        assert "k" in rep.ambiguous  # type: ignore[attr-defined]

    def test_deferred_dimension_is_reported_separately(self, parsed: tuple[object, object]) -> None:
        tbl, rep = parsed
        assert tbl.resolve_dimension("DC", "L5") is None  # type: ignore[attr-defined]
        assert "DC" in rep.unknown_dimension  # type: ignore[attr-defined]
        assert "DC" not in rep.ambiguous  # type: ignore[attr-defined]

    def test_section_boundary_respected(self, parsed: tuple[object, object]) -> None:
        """U.6 之后的内容不得被吸进 U.5 的表。"""
        _, rep = parsed
        assert rep.entries < 20  # type: ignore[attr-defined]


class TestNamespaceResolution:
    def test_same_symbol_different_namespaces(self) -> None:
        """U.5 自己说明歧义: 裸 ``R`` 在两个域含义不同。解析键必须是 (符号, 命名空间)。"""
        src = """\
### U.5 全局符号表（命名空间隔离）

| 符号 | 含义 | 量纲 | 命名空间 |
|---|---|---|---|
| `R` | 电阻 | `[Ω]` | `E` |
| `R` | 可靠度函数 | 无量纲 | `R` |
"""
        tbl, _ = parse_symbol_table(src.splitlines())
        assert tbl.resolve_dimension("R", "E") == parse_spec_dimension("[Ω]")
        assert tbl.resolve_dimension("R", "R") == DIMENSIONLESS
        assert tbl.resolve_dimension("R", "N") is None  # 不猜

    def test_namespace_is_never_guessed_by_prefix(self) -> None:
        """``J`` 命名空间的公式不得命中标着 ``E`` 的条目。"""
        src = """\
### U.5 全局符号表（命名空间隔离）

| 符号 | 含义 | 量纲 | 命名空间 |
|---|---|---|---|
| `x` | 电容纹波 | `[F]` | `E` |
"""
        tbl, _ = parse_symbol_table(src.splitlines())
        assert tbl.resolve_dimension("x", "J") is None

    def test_missing_symbol_returns_none(self) -> None:
        tbl, _ = parse_symbol_table(U5.splitlines())
        assert tbl.resolve_dimension("__不存在__", "E") is None


@pytest.mark.slow
def test_corpus_symbol_table_parses() -> None:
    """真实 U.5 必须抽出条目, 且大部分能定出量纲。"""
    if not SPEC.is_file():
        pytest.skip(f"方案文件不在预期位置: {SPEC}")
    tbl, rep = parse_symbol_table(SPEC.read_text(encoding="utf-8").splitlines())
    assert rep.entries > 100, rep.summary()
    determined = rep.entries - len(rep.unknown_dimension) - len(rep.ambiguous)
    assert determined > 0.7 * rep.entries, rep.summary()
    # 命名空间是这张表的核心, 丢了就退化成全局字典, 歧义无法解析
    assert len({ns for e in tbl.entries for ns in e.namespaces}) >= 10
