"""轨级推导的判据必须是"输出电流", 不能是"量纲为 A 的任何限值"。

背景
----
`_is_load_bearing` 与 `_rated_currents` 曾用「限值单位是 A」作 fallback 判据,
于是保护动作阈值被一起当成"该轨额定满载电流"。PA601 实测 14 个派生场景里 8 个
是错的:

  SR-1309 输出过流保护  动作区间 12~18A, 却挂了 SR-1203 的派生值 7.401A

危害: 7.401A 落在动作区间**之下**, 产测照它设负载根本不触发过流保护 ——
保护功能测不出来, 却显示通过。是静默的功能缺失, 比命名错更严重。

同时它污染额定表: SR-1309 的 max=18A 被当成 -54V 轨额定电流, 封顶基准错
一个量级。所以两处必须同改, 用同一个判据函数。

这几条钉住:
  1. 保护类需求(单位 A)不得产出派生电流;
  2. 保护类需求不得进入额定电流表(否则封顶基准错);
  3. 真正的输出电流需求仍要产出派生电流 —— 不能修过头;
  4. 派生电流必须不超过该轨额定值(量纲判据的回归防线)。

测试用构造条件而非仓库外原文, 保证可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aterag.extract.models import ConditionClause  # noqa: E402
from aterag.extract.models import TestCondition as Cond  # noqa: E402
from aterag.extract.scenarios import (  # noqa: E402
    ScenarioRules,
    _is_load_bearing,
    _rated_currents,
    expand_scenarios,
)


@pytest.fixture(scope="module")
def rules() -> ScenarioRules:
    return ScenarioRules.load(ROOT / "config" / "scenario_rules.yaml")


def _tier_carrier() -> Cond:
    """声明功率分档的整机行 —— 轨级推导的前提。"""
    return Cond(
        req_id="SR-P",
        title="输出功率",
        section_path="4.3.2",
        notes="90~176Vac: 400W; 176~286Vac: 600W",
        limits={"max": 600.0, "unit": "W"},
    )


def _ocp(rail: str, rated: float, req_id: str = "SR-OC") -> Cond:
    """输出电流行 —— 真正该挂派生电流的那类。"""
    return Cond(
        req_id=req_id,
        title="输出电流",
        section_path="4.3.2",
        rail=rail,
        notes="长期工作",
        limits={"min": 0.0, "max": rated, "unit": "A"},
        output_conditions=[
            ConditionClause(kind="output_current", text=f"0~{rated}A", role="output")
        ],
    )


def _protection(rail: str, lo: float | None, hi: float | None, req_id: str) -> Cond:
    """保护条款 —— 单位同为 A, 但 kind 是 protection_action。"""
    lim: dict[str, object] = {"unit": "A", "rail": rail}
    if lo is not None:
        lim["min"] = lo
    if hi is not None:
        lim["max"] = hi
    return Cond(
        req_id=req_id,
        title="输出过流保护",
        section_path="4.3.3",
        rail=rail,
        role="protection_response",
        notes="打隔保护，长期过流不能损坏电源。",
        limits=lim,
        output_conditions=[
            ConditionClause(kind="protection_action", text="过流保护", role="output")
        ],
    )


def test_protection_clause_is_not_load_bearing() -> None:
    """单位为 A 的保护条款不得被判成"输出电流类需求"。

    这条直接钉死回归: 若判据退回量纲, 这里立刻红。
    """
    prot = _protection("-54V", 12.0, 18.0, "SR-OCP")
    assert prot.limits["unit"] == "A", "构造须是电流量纲, 否则本用例失去意义"
    assert not _is_load_bearing(prot), "保护动作点不是额定满载电流"


def test_protection_max_does_not_enter_rated_table() -> None:
    """保护动作点不得进入额定电流表 —— 否则轨级封顶基准错一个量级。

    PA601 的具体后果: -54V 轨额定被记成保护动作点 18A 而非额定 11.1A。
    """
    conds = [_ocp("-54V", 11.1, "SR-OC"), _protection("-54V", 12.0, 18.0, "SR-OCP")]
    rated = _rated_currents(conds)
    assert rated.get("-54V") == 11.1, f"额定表被保护动作点污染: {rated}"


def test_output_current_still_produces_derived(rules: ScenarioRules) -> None:
    """真正的输出电流需求仍要产出派生电流 —— 不能修过头。

    修过头和没修一样坏: 输出电流拿不到分档封顶值, 低压段就会按额定 11.1A
    执行, 击穿 400W 功率上限。
    """
    conds = [_tier_carrier(), _ocp("-54V", 11.1)]
    res = expand_scenarios(conds, rules)
    derived = [s for s in res.scenarios if s.derived]
    assert derived, "输出电流应产出派生电流场景"
    # vals 元素是 ((rail, value), ...) 元组 —— 取唯一那项的 value
    vals = {sorted(s.derived.items())[0][1] for s in derived}
    # 低压档应封顶到 400W 预算, 高压档可用满额定
    assert any(abs(v - 11.1) < 1e-6 for v in vals), f"应有可用满额定的一档: {vals}"
    assert any(v < 11.1 for v in vals), f"应有被功率档封顶的一档: {vals}"


def test_protection_scenario_has_no_derived(rules: ScenarioRules) -> None:
    """保护条款的场景不得带派生电流 —— 判据必须来自规格书自身。"""
    conds = [
        _tier_carrier(),
        _ocp("-54V", 11.1),
        _protection("-54V", 12.0, 18.0, "SR-OCP"),
    ]
    res = expand_scenarios(conds, rules)
    prot = [s for s in res.scenarios if s.req_id == "SR-OCP"]
    assert prot, "保护需求仍应展开出场景(绑档位), 只是不带派生电流"
    for s in prot:
        assert not s.derived, f"保护场景挂了不该有的派生电流: {s.derived}"


def test_derived_never_exceeds_rated(rules: ScenarioRules) -> None:
    """派生电流不得超过该轨额定值 —— 量纲判据的回归防线。

    用一主一辅两轨构造, 辅轨功率更大以检验轨序自动排定 (原先按 priority_rails
    轨名白名单, 换轨名后落进 sorted() 按字母序兜底, 主辅颠倒会算出负电流)。
    """
    conds = [
        _tier_carrier(),
        _ocp("-48V", 20.0, "SR-A"),
        _ocp("12V", 2.0, "SR-B"),
    ]
    res = expand_scenarios(conds, rules)
    derived = [s for s in res.scenarios if s.derived]
    assert derived, "两轨条件下应有派生场景"
    rated = _rated_currents(conds)
    for s in derived:
        for rail, cur in s.derived.items():
            assert cur >= 0.0, f"{s.scenario_id} 派生出负电流 {cur}"
            assert cur <= rated.get(rail, cur) + 1e-3, (
                f"{s.scenario_id} 派生 {cur}A 超过该轨额定 {rated.get(rail)}A"
            )
