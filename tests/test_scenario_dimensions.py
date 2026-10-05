"""场景维度机制的结构性保证 (第 1 步: 让 dimensions 真正生效)。

背景: dimensions 原先是死配置 —— ScenarioRules.dimension_order 被读入并校验
正则, 但 expand_scenarios 全文只引用一个字符串字面量 "ac_input_tier", 其余
维度从未被消费。配置看着齐全, 展开时静默不生效。

这几条用例钉住三件最容易退化的事:
  1. 每个**声明了的**维度都必须出现在产出的 binding 里 (死配置不得复现);
  2. 工况维度必须**逐需求**解析 —— 做成全局池会把无关工况绑到无关需求上
     (ESD 抗扰被标上"温度≤-30℃"), 且场景数成倍膨胀;
  3. 负载的 slew(变化序列)与 level(静态档)语义不同, 序列节点须同时记为档位,
     且档位由序列去重推导, 不独立解析 —— 否则两条正则各认一半。

测试用构造条件而非仓库外的 PA601 原文, 保证可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aterag.extract.models import ConditionClause  # noqa: E402
from aterag.extract.models import TestCondition as Cond  # noqa: E402
from aterag.extract.scenarios import (  # noqa: E402
    ScenarioRules,
    expand_scenarios,
    parse_condition_dimensions,
)


@pytest.fixture(scope="module")
def rules() -> ScenarioRules:
    return ScenarioRules.load(
        Path(__file__).resolve().parents[1] / "config" / "scenario_rules.yaml"
    )


def _by_key(rules: ScenarioRules) -> dict[str, object]:
    return {d.key: d for d in rules.dimensions}


def test_every_declared_dimension_is_expanded(rules: ScenarioRules) -> None:
    """dimensions 声明的每个 key 都必须能在产出的 binding 里出现。

    这条直接防"死配置复现": 若某维度被读入却没进 expand_scenarios 的展开路径,
    它的 key 永远不会出现在 binding 中, 用例即红。
    """
    # 构造覆盖全部维度的条件: 温度窗口 + 负载变化序列 + 电压档。
    conds = [
        Cond(
            req_id="SR-1",
            title="输出功率",
            section_path="4.3.2",
            notes="90~176Vac: 400W; 176~286Vac: 600W",
            limits={"max": 600.0, "unit": "W"},
        ),
        Cond(
            req_id="SR-2",
            title="动态响应恢复时间",
            section_path="4.3.2",
            rail="-54V",
            notes="温度≤-30°时,25%~50%~25%负载变化",
            limits={"max": 200.0, "unit": "us"},
            output_conditions=[ConditionClause(kind="timing", text="200us", role="output")],
        ),
        # 额定电流行: 轨级推导需要它, 否则 _derive_load 无轨可用会 fail-closed。
        Cond(
            req_id="SR-2A",
            title="输出电流",
            section_path="4.3.2",
            rail="-54V",
            notes="长期工作",
            limits={"min": 0.0, "max": 11.1, "unit": "A"},
            output_conditions=[
                ConditionClause(kind="output_current", text="0~11.1A", role="output")
            ],
        ),
        Cond(
            req_id="SR-2B",
            title="额定输出电压",
            section_path="4.3.2",
            rail="-54V",
            notes="上电默认输出",
            limits={"min": -54.0, "unit": "V"},
        ),
    ]
    res = expand_scenarios(conds, rules)
    seen: set[str] = set()
    for s in res.scenarios:
        seen.update(s.bindings)
    declared = {d.key for d in rules.dimensions}
    # 至少 tier/温度/负载三者必须被真正消费
    for key in ("ac_input_tier", "temp_window", "load"):
        assert key in declared, f"配置里应有 {key}"
        assert key in seen, f"维度 {key} 已声明但从未出现在 binding 里 (死配置)"


def test_condition_dimension_is_scoped_to_that_condition(rules: ScenarioRules) -> None:
    """工况维度逐需求解析: 无关需求不得被绑上无关工况。"""
    specs = _by_key(rules)
    noisy = Cond(
        req_id="SR-NOISY",
        title="峰峰值杂音电压",
        section_path="4.3.2",
        rail="-54V",
        notes="温度≤-30°时,纹波放宽至600mV",
        limits={"max": 500.0, "unit": "mV"},
    )
    clean = Cond(
        req_id="SR-CLEAN",
        title="额定输出电压",
        section_path="4.3.2",
        rail="-54V",
        notes="上电默认输出",
        limits={"min": -54.0, "unit": "V"},
    )
    temp = specs["temp_window"]
    assert parse_condition_dimensions(temp, noisy), "有温度窗口的需求应解析出取值"
    assert not parse_condition_dimensions(temp, clean), (
        "无温度窗口的需求不得被绑上温度工况 (会让产测看到一个自己并不成立的工况)"
    )


def test_slew_sequence_records_key_nodes_as_levels(rules: ScenarioRules) -> None:
    """变化序列须同时产出有序路径与去重后的关键节点档位。"""
    load = _by_key(rules)["load"]
    c = Cond(
        req_id="SR-3",
        title="动态响应恢复时间",
        section_path="4.3.2",
        rail="-54V",
        notes="25%~50%~25%或50%~75%~50%负载变化",
    )
    vals = parse_condition_dimensions(load, c)
    seq = [v for v in vals if v.levels]
    assert seq, "应识别出变化序列"
    for v in seq:
        # 路径上的每个节点都必须出现在 levels 里, 且已去重
        nodes = v.text.replace("%", "").split("->")
        assert len(nodes) >= 3, f"序列应含起止与中间节点: {v.text}"
        assert len(v.levels) == len(set(v.levels)), f"节点档位应去重: {v.levels}"
        for n in dict.fromkeys(nodes):
            assert any(n in lv for lv in v.levels), (
                f"路径节点 {n}% 未记入档位 {v.levels} —— 会导致路径里有而档位表里没有"
            )


def test_level_and_slew_are_distinct_values(rules: ScenarioRules) -> None:
    """静态档与变化序列是两个取值, 不能混成同一个。

    SR-1210 "50%最大输出负载" 是静态档(判效率 91%), SR-1211 "50%负载变化"
    是序列(判 200us 响应)。混成一个会让产测拿"50%"这个点去判动态响应。
    """
    load = _by_key(rules)["load"]
    c = Cond(
        req_id="SR-4",
        title="动态响应恢复时间",
        section_path="4.3.2",
        rail="-54V",
        notes="50%~75%~50%负载变化",
    )
    vals = parse_condition_dimensions(load, c)
    texts = {v.text for v in vals}
    assert any("->" in t for t in texts), f"应有序列形态取值: {texts}"


def test_dimension_values_come_from_spec_text(rules: ScenarioRules) -> None:
    """取值数值必须全部来自原文 —— 代码不生成任何工况数字。"""
    load = _by_key(rules)["load"]
    c = Cond(
        req_id="SR-5",
        title="整机效率",
        section_path="4.3.2",
        notes="额定220Vac 输入,20%最大输出负载",
    )
    vals = parse_dimension_all(load, c)
    assert vals, "应解析出负载档"
    for v in vals:
        assert v.source_text, "每个取值必须带溯源片段"


def parse_dimension_all(spec, c):  # noqa: ANN001, ANN201
    return parse_condition_dimensions(spec, c)


def test_unknown_dimension_in_order_fails_closed(rules: ScenarioRules) -> None:
    """ordering.dimension_order 引用未声明维度时必须报错, 不能静默忽略。"""
    broken = ScenarioRules(
        dimensions=rules.dimensions,
        tier_pattern=rules.tier_pattern,
        tier_from_notes=rules.tier_from_notes,
        tier_from_requirement_title=rules.tier_from_requirement_title,
        derivations=rules.derivations,
        dimension_order=("no_such_dimension",),
        sort_ascending=rules.sort_ascending,
        naming=rules.naming,
    )
    conds = [
        Cond(
            req_id="SR-6",
            title="输出功率",
            section_path="4.3.2",
            notes="90~176Vac: 400W",
            limits={"max": 400.0, "unit": "W"},
        )
    ]
    with pytest.raises(ValueError, match="未声明的维度"):
        expand_scenarios(conds, broken)


def test_dimension_without_value_source_fails_config_check(rules: ScenarioRules) -> None:
    """维度既无 pattern 也无 modes -> 配置校验必须报错。

    这类配置解析恒为空, 展开时静默不生效, 是最难排查的"配置写了但没生效"。
    """
    import dataclasses

    specs = list(rules.dimensions)
    broken_spec = dataclasses.replace(
        specs[1], key="broken_dim", carrier="notes_span", pattern="", modes=()
    )
    broken = ScenarioRules(
        dimensions=[*specs, broken_spec],
        tier_pattern=rules.tier_pattern,
        tier_from_notes=rules.tier_from_notes,
        tier_from_requirement_title=rules.tier_from_requirement_title,
        derivations=rules.derivations,
        dimension_order=rules.dimension_order,
        sort_ascending=rules.sort_ascending,
        naming=rules.naming,
    )
    with pytest.raises(ValueError, match="取值无来源"):
        broken.validate()
