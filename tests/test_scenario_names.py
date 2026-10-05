"""场景名必须能分辨测点 (第 2 步: 工况维度进名字)。

背景
----
PA601 实测出**同名但判据不同**的场景: SR-1203 输出电流在 90~176Vac 档封顶
7.401A, 在 176~286Vac 档是额定 11.1A, 相差 50%, 而两个场景的名字完全相同
("长期工作输出电流@-54V")。产测按名字执行必然有一个测点用错限值, 且不报错。

场景名是产测人员唯一的辨识依据(requirement_id 只在系统内部流转), 所以名字
唯一是硬要求, 不是体验优化。

这几条用例钉住四件事:
  1. 同名场景的判据不得不同 —— 名字相同就意味着产测无法分辨, 属缺陷;
  2. 工况档必须出现在名字里 —— 名字里的档位文本来自规格书原文;
  3. 档位文本不得由代码构造 —— 代码里不能出现 PA601 的档位数字;
  4. 单值维度不加标签 —— 否则名字被无意义的标签撑长。

测试用构造条件而非仓库外的 PA601 原文, 保证可复现。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aterag.extract.models import ConditionClause  # noqa: E402
from aterag.extract.models import TestCondition as Cond  # noqa: E402
from aterag.extract.scenarios import ScenarioRules, expand_scenarios  # noqa: E402


@pytest.fixture(scope="module")
def rules() -> ScenarioRules:
    return ScenarioRules.load(ROOT / "config" / "scenario_rules.yaml")


def _conds() -> list[Cond]:
    """构造会分档的需求: 输出电流(受功率档封顶) + 带温度窗口的动态响应。"""
    return [
        Cond(
            req_id="SR-1A",
            title="输出功率",
            section_path="4.3.2",
            notes="90~176Vac: 400W; 176~286Vac: 600W",
            limits={"max": 600.0, "unit": "W"},
        ),
        Cond(
            req_id="SR-1",
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
            req_id="SR-2",
            title="动态响应恢复时间",
            section_path="4.3.2",
            rail="-54V",
            notes="温度≥-25℃:≤6s; 温度≤-25℃:≤12s",  # 温度窗口取自 notes
            limits={"max": 200.0, "unit": "us"},
            output_conditions=[ConditionClause(kind="timing", text="200us", role="output")],
        ),
    ]


def test_scenario_names_are_globally_unique(rules: ScenarioRules) -> None:
    """展开后场景名全局唯一 —— 重名等于让产测按名字瞎测。

    这条直接钉死回归: 维度标签或去重后缀任一失效, 名字就会重名。
    """
    res = expand_scenarios(_conds(), rules)
    names = [s.name for s in res.scenarios]
    assert names, "构造的条件应至少展开出一个场景"
    dup = sorted({n for n in names if names.count(n) > 1})
    assert not dup, f"场景名重复: {dup}"


def test_same_name_never_carries_different_verdict(rules: ScenarioRules) -> None:
    """同名场景的判据必须一致 —— 判据不同却同名是危害最大的形态。

    比"重名"更严重: 重名但判据相同只是难分辨, 判据不同则产测必然用错限值。
    """
    res = expand_scenarios(_conds(), rules)
    by_name: dict[str, set[str]] = {}
    for s in res.scenarios:
        key = ",".join(f"{k}={v}" for k, v in sorted(s.derived.items()))
        by_name.setdefault(s.name, set()).add(key)
    bad = {n: v for n, v in by_name.items() if len(v) > 1}
    assert not bad, f"同名场景判据不同: {bad}"


def test_dimension_tier_appears_in_name(rules: ScenarioRules) -> None:
    """电压档必须进名字 —— 它决定派生电流, 是判据的一部分。"""
    res = expand_scenarios(_conds(), rules)
    currents = [s for s in res.scenarios if s.req_id == "SR-1"]
    assert len(currents) >= 2, "输出电流应按电压档展开"
    for s in currents:
        assert s.name != "长期工作输出电流", f"名字未带工况档: {s.name!r}"
        assert "[" in s.name and "]" in s.name, f"缺少工况标签: {s.name!r}"


def test_temp_window_appears_in_name(rules: ScenarioRules) -> None:
    """温度窗口必须进名字 —— 6s 与 12s 是两个判据。"""
    res = expand_scenarios(_conds(), rules)
    temps = [s for s in res.scenarios if s.req_id == "SR-2"]
    # 有轨需求与电压档做笛卡尔积, 故场景数 = 温度档数 x 档位数;
    # 这里要验的是"温度档各自出现在名字里", 而不是场景总数。
    labels = {s.bindings.get("temp_window") for s in temps}
    assert len(labels) == 2, f"温度窗口应解析出 2 个取值, 实得 {labels}"
    for s in temps:
        assert s.bindings["temp_window"] in s.name, (
            f"温度档未出现在名字里: {s.name!r} <- {s.bindings['temp_window']!r}"
        )
    # 同档不同温度 -> 名字必须不同 (否则 6s 与 12s 两个判据同名)
    same_tier: dict[str, set[str]] = {}
    for s in temps:
        same_tier.setdefault(s.bindings.get("ac_input_tier", ""), set()).add(s.name)
    for tier, ns in same_tier.items():
        assert len(ns) == len(labels), f"档 {tier} 下温度档名字未区分: {ns}"


def test_name_text_comes_from_spec_not_code(rules: ScenarioRules) -> None:
    """名字里的档位文本必须逐字来自规格书解析结果, 不是代码拼的。

    做法: 改构造条件里的档位数字, 名字必须跟着变 —— 证明文本是数据驱动而非
    硬编码常量。
    """
    conds = _conds()
    base = expand_scenarios(conds, rules)
    base_names = {s.name for s in base.scenarios}

    # 换一个完全不同的电压窗口, 名字必须随之改变
    conds2 = _conds()
    conds2[0].notes = "100~240Vac: 350W; 240~260Vac: 500W"
    changed = expand_scenarios(conds2, rules)
    new_names = {s.name for s in changed.scenarios}
    assert base_names != new_names, "改档位数字后场景名未变 —— 名字可能来自硬编码"
    # 新档位文本应出现在名字里
    assert any("100~240Vac" in n for n in new_names), f"新档位未进名字: {sorted(new_names)}"
    assert not any("90~176Vac" in n for n in new_names), "旧档位残留"


def test_source_has_no_pa601_tier_literals() -> None:
    """源码中不得出现 PA601 的档位/轨名字面量 —— 档位只能来自规格书原文。

    注释与文档字符串提及是历史说明, 不算硬编码; 只有参与逻辑的字面量才是。
    """
    src = (ROOT / "src" / "aterag" / "extract" / "scenarios.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                doc_ids.add(id(first.value))
    # 档位文本形如 "90~176Vac" / 轨名形如 "-54V" / "3.45V"
    banned = ("90~176", "176~286", "-54V", "3.45V", "11.1", "7.401")
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in doc_ids:
                continue
            for b in banned:
                if b in node.value:
                    hits.append(f"L{node.lineno}: {node.value!r} 含 {b!r}")
    assert not hits, "源码中出现规格书档位/轨名字面量: " + "; ".join(hits)


def test_dimension_label_must_reference_declared_dimension(
    rules: ScenarioRules,
) -> None:
    """标签引用未声明维度 -> 配置校验必须报错 (防标签成死配置)。"""
    import dataclasses

    bad_label = dataclasses.replace(
        rules.naming.dimension_labels[0], id="dl_ghost", key="no_such_dim"
    )
    naming = dataclasses.replace(
        rules.naming, dimension_labels=(bad_label, *rules.naming.dimension_labels[1:])
    )
    broken = dataclasses.replace(rules, naming=naming)
    with pytest.raises(ValueError, match="未声明的维度"):
        broken.validate()


def test_load_dimension_not_declared_as_label(rules: ScenarioRules) -> None:
    """load 走 load_levels 前缀机制, 不得再作为标签声明。

    两条路径都渲染会出现 "20%载20%载效率" 这种重复名字。
    """
    keys = {dl.key for dl in rules.naming.dimension_labels}
    assert "load" not in keys, "load 已由 load_levels 命名, 再声明标签会重复"
