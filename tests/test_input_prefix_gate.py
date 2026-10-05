"""输入侧命名闸门 —— 命名必须反映"被测对象是什么量"。

背景
----
`naming.input_levels` 的 when 原本只看"限值列有几个键"(limits_only / limits_all),
不看被测对象是什么量。输出电压(min=3.45)、纹波(max=500mV)、上升时间(max=20ms)
恰好只有 min 或 max 一个键, 于是被输入前缀规则接走:

  额定输出电压  ->  输入最小值@-54V     (实际该设输出侧激励, 不是输入电压)
  峰峰值杂音    ->  输入最大值@3.45V
  上升时间      ->  输入最大值@-54V

PA601 实测 23 个输入侧命名里 17 个是这种错。危害是产测照名字设输入电压,
测到的却不是被测对象。

修法是加 limit_kind_is 闸: 限值列对应的量纲必须属于输入侧
(input_voltage / input_frequency)。该值由装配层写入 ConditionClause.kind
(source=limits), 命名层复用而不重新实现单位→kind 映射。

这几条钉住:
  1. 输出量纲不得拿到输入侧前缀 (核心回归);
  2. 输入量纲必须能拿到输入侧前缀 (防修过头 —— 全不放行会让输入电压
     范围条款失去可读名字);
  3. 闸门按语义判定而非单位字面量 (V 既可能是输出电压, 也可能出现在
     输出功率分档备注里);
  4. 无量纲信号类条款不得拿到输入侧前缀。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aterag.extract.models import ConditionClause  # noqa: E402
from aterag.extract.models import TestCondition as Cond  # noqa: E402
from aterag.extract.scenarios import ScenarioRules  # noqa: E402


@pytest.fixture(scope="module")
def rules() -> ScenarioRules:
    return ScenarioRules.load(ROOT / "config" / "scenario_rules.yaml")


def _cond(
    req_id: str,
    title: str,
    *,
    unit: str,
    limits: dict,
    limit_kind: str,
    rail: str = "",
    role: str = "output_spec",
    section: str = "4.3.2",
) -> Cond:
    """构造一条限值在 SRC_LIMITS 子句上的条款。

    limit_kind 即装配层会写入的子句 kind, 与 limit_kinds 的映射结果对应。
    """
    out = [ConditionClause(kind=limit_kind, text="限值", role="output", source="limits")]
    inp: list[ConditionClause] = []
    if limit_kind == "input_voltage":
        inp.append(
            ConditionClause(
                kind="input_voltage", text="额定输入", role="input", source="notes",
                value={"unit": unit},
            )
        )
    elif limit_kind == "input_frequency":
        inp.append(
            ConditionClause(
                kind="input_frequency", text="交流输入频率", role="input", source="limits",
                value={"min": limits.get("min"), "max": limits.get("max"), "unit": unit},
            )
        )
    return Cond(
        req_id=req_id,
        title=title,
        section_path=section,
        role=role,
        rail=rail,
        limits={**limits, "unit": unit, "rail": rail},
        input_conditions=inp,
        output_conditions=out,
    )


INPUT_PREFIXES = ("输入最小值", "输入最大值", "输入典型值", "输入极限值", "输入范围")


def _name(cond: Cond, rules: ScenarioRules, rail: str = "") -> str:
    from aterag.extract.scenarios import derive_scenario_name

    return derive_scenario_name(cond, rules.naming, rail)


def test_output_voltage_must_not_get_input_prefix(rules: ScenarioRules) -> None:
    """输出电压 (unit=V) 只有 min 一个键, 不得被命名成"输入最小值"。

    这是核心回归: 修复前该条件命中 il_min, 名字是"输入最小值@-54V"。
    """
    c = _cond(
        "SR-V",
        "额定输出电压",
        unit="V",
        limits={"min": -54.0},
        limit_kind="output_voltage",
        rail="-54V",
    )
    name = _name(c, rules, rail="-54V")
    assert not name.startswith(INPUT_PREFIXES), f"输出电压被误命名成输入侧: {name!r}"
    assert "-54V" in name, f"应保留轨后缀: {name!r}"


def test_ripple_must_not_get_input_prefix(rules: ScenarioRules) -> None:
    """纹波 (unit=mV) 只有 max 一个键, 不得被命名成"输入最大值"。"""
    c = _cond(
        "SR-R",
        "峰峰值杂音电压",
        unit="mV",
        limits={"max": 500.0},
        limit_kind="ripple",
        rail="-54V",
    )
    name = _name(c, rules, rail="-54V")
    assert not name.startswith(INPUT_PREFIXES), f"纹波被误命名成输入侧: {name!r}"


def test_timing_must_not_get_input_prefix(rules: ScenarioRules) -> None:
    """上升时间 (unit=ms) 不得被命名成输入侧 —— 测的是时间不是电压。"""
    c = _cond(
        "SR-T",
        "输出电压上升时间",
        unit="ms",
        limits={"min": 0.3, "max": 20.0},
        limit_kind="timing",
        rail="3.45V",
    )
    name = _name(c, rules, rail="3.45V")
    assert not name.startswith(INPUT_PREFIXES), f"时间量被误命名成输入侧: {name!r}"


def test_power_factor_must_not_get_input_prefix(rules: ScenarioRules) -> None:
    """功率因数 (无量纲, unit='-') 不得被命名成"输入最小值"。

    功率因数在本项目的 kinds 词表里归 OUTPUT 类 —— 它是被测出来的性能指标,
    不是可设定的输入条件。故走 title_kinds 推断出 power_factor 后,
    闸门正确放行(不放行)。
    """
    c = _cond(
        "SR-PF",
        "功率因数",
        unit="-",
        limits={"min": 0.98},
        limit_kind="power_factor",
        role="input_domain",
        section="4.3.1",
    )
    name = _name(c, rules)
    assert not name.startswith(INPUT_PREFIXES), f"功率因数被误命名成输入侧: {name!r}"


def test_input_voltage_still_gets_input_prefix(rules: ScenarioRules) -> None:
    """输入电压范围仍须拿到输入侧前缀 —— 防修过头。

    全不放行会让输入特性表的条款失去可读名字, 退回标题原文。
    """
    c = _cond(
        "SR-IV",
        "标称输入电压范围",
        unit="Vac",
        limits={"min": 200.0, "typ": 220.0, "max": 240.0},
        limit_kind="input_voltage",
        role="input_domain",
        section="4.3.1",
    )
    name = _name(c, rules)
    assert name.startswith(INPUT_PREFIXES), f"输入电压未拿到输入侧前缀: {name!r}"
    # min+typ+max 三键齐备时命中 il_typ, 措辞是"输入典型值"(不带数值)——
    # 产测看的是"测标称点"这个动作, 具体数值在 limits 里。
    # 带数值的区间措辞由 il_range 覆盖 (见 test_input_frequency_gets_range_prefix)。
    assert name == "输入典型值", f"三键齐备应命名为典型值, 实得 {name!r}"


def test_input_frequency_gets_range_prefix(rules: ScenarioRules) -> None:
    """输入频率用通用区间措辞 (需求方确认纳入)。

    il_range 的措辞 "输入范围{min}~{max}{unit}" 天然涵盖频率, 不必单造措辞。
    """
    c = _cond(
        "SR-IF",
        "交流输入频率",
        unit="Hz",
        limits={"min": 45.0, "max": 66.0},
        limit_kind="input_frequency",
        role="input_domain",
        section="4.3.1",
    )
    name = _name(c, rules)
    assert name.startswith("输入范围"), f"输入频率未拿到区间前缀: {name!r}"
    assert "45" in name and "66" in name and "Hz" in name, name


def test_signal_clause_without_limits_gets_no_prefix(rules: ScenarioRules) -> None:
    """无量纲信号类 (limit_kind 为空) 不得拿到输入侧前缀。"""
    c = Cond(
        req_id="SR-SIG",
        title="电源在位",
        section_path="4.3.4.1",
        role="signal_io",
        limits={"unit": "", "rail": ""},
        output_conditions=[
            ConditionClause(kind="signal_state", text="在位", role="output", source="title")
        ],
    )
    name = _name(c, rules)
    assert not name.startswith(INPUT_PREFIXES), f"信号类被误命名成输入侧: {name!r}"


def test_gate_config_only_allows_input_side_kinds(rules: ScenarioRules) -> None:
    """配置自检: limit_kind_is 只能引用 kinds.input 词表内的值。

    引用输出侧 kind 会让闸门形同虚设 (输出量纲被放进白名单)。
    """
    _cp = yaml.safe_load((ROOT / "config" / "condition_patterns.yaml").read_text(encoding="utf-8"))
    input_vocab = set(_cp["kinds"]["input"])
    for r in rules.naming.input_levels:
        for k in r.when.get("limit_kind_is", []):
            assert k in input_vocab, f"{r.id}: {k!r} 不在 kinds.input 词表"


def test_gate_does_not_rely_on_unit_literals() -> None:
    """闸门不得退回按单位字面量判定。

    unit_is 曾只看单位字面量, 而 V 既可能是输出电压(unit=V), 也可能出现在
    输出功率分档备注里。改判语义(limit_kind)后, 需确认配置里不再有 unit_is。
    """
    _cfg = yaml.safe_load((ROOT / "config" / "scenario_rules.yaml").read_text(encoding="utf-8"))
    for r in _cfg["naming"]["input_levels"]:
        assert "unit_is" not in r["when"], f"{r['id']} 仍依赖 unit_is 字面量判定"
        assert "limit_kind_is" in r["when"], f"{r['id']} 缺 limit_kind_is 闸"
