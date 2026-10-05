"""分档限值: 备注里的「条件式 + 区间」必须按工况档解析到场景上。

问题
----
同一指标的判据可能随工况档变化, 而规格书常把替代区间**只写在备注里**, 表格的
最小值/最大值列给的是默认档的值。PA601 SR-1309 输出过流保护:

    表格列(默认档)  min=12A  max=18A
    备注            "输入电压<176Vac, 过流点8.1A~18A"

12A 只在 >=176Vac 成立。不解析这条备注, 低压档场景会沿用 12A —— 产测按 12A 设
激励, 永远测不到低压段的下边界(8.1A), 而该下边界正是规格书特意给出来的。

关键约束
--------
备注里的条件式是**原文表述**(「输入电压<176Vac」), 档位取值来自**另一条需求**
(SR-1204 的功率分档 "90~176Vac; 176~286Vac")。两者来源不同, 不能在解析期互相
假设 —— 解析期还不知道档位分没分。所以:
  * 解析期只保留条件式的维度/方向/阈值, 不猜它指哪个档;
  * 场景展开时按本场景的 bindings 对上, 对不上就用基准值(宁可用基准, 不猜)。

这组用例钉住:
  1. 命中档拿到替代值, 未命中档保留基准值(核心);
  2. 判定是「档位整体落入条件区间」而非「边界相交」—— 后者会让相邻两档同时命中,
     基准值永远用不上;
  3. 条件式跨在档位中间时不命中(宁可用基准, 不猜);
  4. 只改对应端点, 未改端保留基准(规格书常只改一端);
  5. 条件式与档位都来自配置/规格书, 代码不含具体数字;
  6. 导出契约: 需求侧带 variants(可审计), 场景侧带已解析的 limits。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aterag.extract.models import ConditionClause  # noqa: E402
from aterag.extract.models import TestCondition as Cond  # noqa: E402
from aterag.extract.scenarios import (  # noqa: E402
    ScenarioRules,
    _guard_holds,
    expand_scenarios,
    resolve_scenario_limits,
)


def _guard(thr: float, op: str = "<") -> dict[str, Any]:
    """构造条件式。数值取自 PA601 实测, 但仅作测试输入, 不进生产代码。"""
    return {
        "dimension": "ac_input_tier",
        "op": op,
        "value": thr,
        "source_text": "输入电压<176Vac，过流点8.1A~18A",
    }


def _cond(guard: dict[str, Any] | None, lo: float = 12.0, hi: float = 18.0) -> Cond:
    clauses = [
        ConditionClause(
            kind="protection_action",
            text="保护动作",
            role="output",
            value={"min": lo, "max": hi, "unit": "A"},
        )
    ]
    if guard:
        clauses.append(
            ConditionClause(
                kind="protection_action",
                text="分档动作点",
                role="output",
                value={"value": 8.1, "value2": 18.0, "unit": "A", "guard": guard},
            )
        )
    return Cond(
        req_id="SR-1",
        title="输出过流保护",
        section_path="4.3.3",
        rail="-54V",
        notes="-",
        limits={"min": lo, "max": hi, "unit": "A", "rail": "-54V"},
        output_conditions=clauses,
    )


# --------------------------------------------------------------------------
# _guard_holds: 条件式与档位的对上规则
# --------------------------------------------------------------------------


def test_low_tier_entirely_below_threshold_holds() -> None:
    """档位上界不超过阈值 -> 命中(低压段整体落在「低于阈值」里)。"""
    assert _guard_holds(_guard(176.0), {"ac_input_tier": "90~176Vac"}) is True


def test_high_tier_does_not_hold() -> None:
    """档位下界在阈值之上 -> 不命中(高压段用基准值)。"""
    assert _guard_holds(_guard(176.0), {"ac_input_tier": "176~286Vac"}) is False


def test_tier_straddling_threshold_does_not_hold() -> None:
    """档位跨在阈值两侧 -> 不命中。

    条件式说的是"低于某阈值时用另一个限值", 档位若横跨两侧就说不清该用哪个。
    此时宁可用基准值, 也不猜 —— 猜错会让某一档的保护判据整体错位。
    """
    assert _guard_holds(_guard(176.0), {"ac_input_tier": "100~250Vac"}) is False


def test_boundary_touching_does_not_hit_both_neighbours() -> None:
    """档位边界与阈值重合时, 不得让相邻两档同时命中。

    用"边界相交"判定的话, 90~176 与 120~176 都会命中, 基准值(高压段)就永远
    用不上 —— 于是 176~286 之外的所有档都被当成低压档。
    """
    g = _guard(176.0)
    assert _guard_holds(g, {"ac_input_tier": "90~176Vac"}) is True
    assert _guard_holds(g, {"ac_input_tier": "176~286Vac"}) is False


def test_greater_than_direction() -> None:
    """反向条件式(高于某阈值)按档位下界判定。"""
    g = _guard(176.0, op=">")
    assert _guard_holds(g, {"ac_input_tier": "176~286Vac"}) is True
    assert _guard_holds(g, {"ac_input_tier": "90~176Vac"}) is False


def test_unknown_dimension_or_non_range_binding_never_holds() -> None:
    """维度未绑定 / 绑定值不是区间 / 阈值非数值 -> 一律不命中(退回基准值)。"""
    assert _guard_holds(_guard(176.0), {}) is False
    assert _guard_holds(_guard(176.0), {"ac_input_tier": "满载"}) is False
    assert _guard_holds(_guard(176.0), {"other_dim": "90~176Vac"}) is False
    assert _guard_holds(
        {"dimension": "ac_input_tier", "op": "<", "value": None},
        {"ac_input_tier": "90~176Vac"},
    ) is False


# --------------------------------------------------------------------------
# resolve_scenario_limits: 按绑定解析
# --------------------------------------------------------------------------


def test_matching_binding_gets_override() -> None:
    lim, basis = resolve_scenario_limits(_cond(_guard(176.0)), {"ac_input_tier": "90~176Vac"})
    assert lim["min"] == pytest.approx(8.1)
    assert lim["max"] == pytest.approx(18.0)
    assert basis, "命中替代限值必须给出原文依据供追溯"


def test_non_matching_binding_keeps_base() -> None:
    lim, basis = resolve_scenario_limits(_cond(_guard(176.0)), {"ac_input_tier": "176~286Vac"})
    assert lim["min"] == pytest.approx(12.0)
    assert lim["max"] == pytest.approx(18.0)
    assert basis == "", "未命中时不应伪造依据"


def test_only_declared_endpoints_are_overridden() -> None:
    """只覆盖条件式给出的端点, 未给出的保留基准。

    规格书常只改一端(低压段把下限 12A 降到 8.1A, 上限两档相同), 若整体替换会把
    未提及的那端也改掉, 凭空造出规格书没写的限值。
    """
    cond = Cond(
        req_id="SR-2",
        title="输出过压保护",
        section_path="4.3.3",
        rail="-54V",
        notes="-",
        limits={"min": 58.0, "max": 63.0, "unit": "V"},
        output_conditions=[
            ConditionClause(
                kind="protection_action",
                text="仅低温放宽上限",
                role="output",
                # 只给出 max, 没有 value(min) -> 只该改上限
                value={"value2": 68.0, "guard": _guard(176.0)},
            )
        ],
    )
    lim, basis = resolve_scenario_limits(cond, {"ac_input_tier": "90~176Vac"})
    assert lim["min"] == pytest.approx(58.0), "未给出的端点必须保留基准"
    assert lim["max"] == pytest.approx(68.0)
    assert basis


def test_no_guard_clause_returns_base_untouched() -> None:
    cond = _cond(None)
    lim, basis = resolve_scenario_limits(cond, {"ac_input_tier": "90~176Vac"})
    assert lim == cond.limits
    assert basis == ""


# --------------------------------------------------------------------------
# 展开接线
# --------------------------------------------------------------------------


def test_expansion_resolves_per_binding() -> None:
    """场景展开后, 各档场景带自己的限值。

    这是本组的核心端到端断言: 若接线漏了, 所有场景都拿基准 12A, 低压段的
    8.1A 下边界就丢了 —— 而这正是要修的缺陷本身。
    """
    rules = ScenarioRules.load(ROOT / "config" / "scenario_rules.yaml")
    conds = [
        Cond(
            req_id="SR-P",
            title="输出功率",
            section_path="4.3.2",
            notes="90~176Vac: 400W; 176~286Vac: 600W",
            limits={"max": 600.0, "unit": "W"},
        ),
        # 额定电流行: 轨级推导需要它(否则 _derive_load 的 fail-closed 会先抛),
        # 与本用例要验的判据解析无关。
        Cond(
            req_id="SR-I",
            title="输出电流",
            section_path="4.3.2",
            rail="-54V",
            notes="长期工作",
            limits={"min": 0.0, "max": 11.1, "unit": "A"},
            output_conditions=[
                ConditionClause(kind="output_current", text="0~11.1A", role="output")
            ],
        ),
        _cond(_guard(176.0)),
    ]
    res = expand_scenarios(conds, rules)
    got = {
        s.bindings.get("ac_input_tier"): s.limits.get("min")
        for s in res.scenarios
        if s.req_id == "SR-1"
    }
    assert got.get("90~176Vac") == pytest.approx(8.1), "低压档应用自己的下限"
    assert got.get("176~286Vac") == pytest.approx(12.0), "高压档应保留基准下限"


def test_export_carries_variants_and_resolved_limits() -> None:
    """导出契约: 需求侧带 variants(可审计), 场景侧带已解析的 limits。"""
    from aterag.extract.bundle import requirement_to_model

    cond = _cond(_guard(176.0))
    scen = [
        type(
            "S",
            (),
            {
                "scenario_id": "S1",
                "seq": 0,
                "name": "n",
                "rail": "-54V",
                "bindings": {"ac_input_tier": "90~176Vac"},
                "derived": {},
                "limits": {"min": 8.1},
                "limit_basis": "src",
                "basis": "b",
                "source": "spec",
            },
        )()
    ]
    d = requirement_to_model(cond, scen).model_dump()
    assert d["limits"] and d["limits"][0]["variants"], "需求侧应导出分档限值供审计"
    assert d["scenarios"][0]["limits"] == {"min": 8.1}, "场景侧应带已解析的限值"
    assert d["scenarios"][0]["limit_basis"] == "src"


# --------------------------------------------------------------------------
# 通用性
# --------------------------------------------------------------------------


def test_no_spec_numbers_in_code() -> None:
    """代码不得含条件式阈值或档位数字 —— 全部来自规格书与配置。"""
    src = (ROOT / "src" / "aterag" / "extract" / "scenarios.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                doc_nodes.add(id(first.value))
    lits = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, (int, float))
        and not isinstance(n.value, bool)
        and id(n) not in doc_nodes
    ]
    for num in (176.0, 8.1, 90, 286):
        assert num not in lits, f"代码里出现了规格书数字字面量: {num}"
