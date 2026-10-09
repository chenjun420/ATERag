"""轨级负载推导的 fail-closed 保证。

背景: 输出功率按输入电压分档(PA601 SR-1204 "90~176Vac: 400W;
176~286Vac: 600W"), 所以各轨满载电流受档位封顶 —— -54V 轨额定 11.1A
只在 >=176Vac 成立, 400W 档下实际只有 7.401A。

推导要按轨分配功率才能得出这个结论。原先无可用轨时 _derive_load 静默返回
空字典, 调用方据此以为"该档无需推导", 场景便直接采用额定电流当判据 ——
产线按 11.1A 设低压段负载, 400W 限值被击穿, 且不报任何错。

这三条用例把"必须报错"钉死。构造输入而非依赖仓库外规格书原文, 保证可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aterag.extract.scenarios import (  # noqa: E402
    ScenarioRules,
    Tier,
    _derive_load,
)


@pytest.fixture(scope="module")
def rules() -> ScenarioRules:
    return ScenarioRules.load(
        Path(__file__).resolve().parents[1] / "config" / "scenario_rules.yaml"
    )


def test_no_rail_available_raises(rules: ScenarioRules) -> None:
    """轨名与电压均缺失时必须抛错, 不能静默返回空 (最直接的失效场景)。"""
    with pytest.raises(ValueError, match="无可用轨"):
        _derive_load(rules, [], {}, {})


def test_rated_without_voltage_raises(rules: ScenarioRules) -> None:
    """有额定电流但无轨电压 -> 公式除以 U, 不可推导, 必须报错。"""
    with pytest.raises(ValueError, match="无可用轨"):
        _derive_load(rules, [], {"-54V": 11.1}, {})


def test_disjoint_rails_raises(rules: ScenarioRules) -> None:
    """额定与电压的轨名不重叠 -> 无任何轨同时满足两个条件, 必须报错。

    换轨名(如规格书改轨名而规则未同步)最容易走到这里: 不报错就会算出一个
    看似正常的数, 而它与规格书无关。
    """
    with pytest.raises(ValueError, match="无可用轨"):
        _derive_load(rules, [], {"-54V": 11.1}, {"3.45V": 3.45})


def test_error_message_reports_available_rails(rules: ScenarioRules) -> None:
    """报错须带出实际可用轨与电压, 否则换型号时无从判断该改哪一侧。"""
    with pytest.raises(ValueError) as exc:
        _derive_load(rules, [], {"-12V": 25.0}, {"3.3V": 3.3})
    msg = str(exc.value)
    assert "available_rated=" in msg
    assert "available_volts=" in msg


def test_available_rail_still_derives(rules: ScenarioRules) -> None:
    """回归对照: 轨齐备时仍能正常推导, fail-closed 没有误伤正常路径。"""
    tiers = [Tier(min_vac=90.0, max_vac=176.0, power_w=400.0, source_text="90~176Vac: 400W")]
    out = _derive_load(rules, tiers, {"-54V": 11.1, "3.45V": 0.1}, {"-54V": 54.0, "3.45V": 3.45})
    key = (90.0, 176.0, 400.0)
    assert key in out
    assert out[key]["-54V"] == pytest.approx(7.401, abs=1e-3)
