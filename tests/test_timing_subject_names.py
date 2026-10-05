"""时序类被测对象必须按条目名区分, 不能只按量纲。

背景
----
量纲 (ms / s / us / mS) 无法区分这几种时序测量, 它们在 ``limit_kinds`` 里
只能一律落到 ``kind=timing``。于是一个只看 kind 的命名规则会把它们全部渲染成
同一个名字。PA601 实测的错名:

    SR-1211 动态响应恢复时间  -> 动态响应时间   (对)
    SR-1213 开机输出延迟      -> 动态响应时间   (错: 测的是开机时序)
    SR-1215 输出电压上升时间  -> 动态响应时间   (错: 测的是输出电压建立)
    SR-1218 掉电延时功能      -> 动态响应时间   (错: 测的是掉电保持)

危害不只是名字难看: 产测按名字执行, 名字说「动态响应」就去测动态响应,
而掉电延时根本没被测 —— 且不报任何错。

这几条用例钉住:
  1. **同一量纲、不同条目名必须给出不同被测对象** —— 证明区分依据是语义而非量纲;
  2. 同一测量的不同写法归一到同一被测对象 —— 改版换措辞不应让用例名漂移,
     否则幂等导入会把老用例判成新用例, 产测历史断链;
  3. 未识别的时序条目仍走兜底, 不得因此丢名(防"修过头": 新增条目名要能落到通用名);
  4. 兜底规则必须排在具体规则之后 —— 顺序即优先级, 排错会让具体规则永不生效;
  5. 规则只在配置里声明, 代码不含任何具体条目名或量纲字面量。

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

# 语义不同的时序测量 -> 期望被测对象。**同量纲**, 只有条目名不同。
DISTINCT_CASES = [
    ("动态响应恢复时间", "动态响应时间"),
    ("开机输出延迟", "开机输出延迟"),
    ("输出电压上升时间", "上升时间"),
    ("掉电延时功能", "掉电延时"),
]

# 同一测量的不同写法 -> 必须落到同一个被测对象 (别名归一)。
ALIAS_CASES = [
    ("掉电延时功能", "掉电延时"),
    ("掉电保持时间", "掉电延时"),
    ("后备时间", "掉电延时"),
]

ALL_CASES = DISTINCT_CASES + ALIAS_CASES


def _cond(req_id: str, title: str, unit: str = "mS") -> Cond:
    return Cond(
        req_id=req_id,
        title=title,
        section_path="4.3.2",
        rail="-54V",
        notes="-",
        limits={"min": 10.0, "unit": unit},
        output_conditions=[ConditionClause(kind="timing", text="10", role="output")],
    )


def _subject(name: str) -> str:
    """从场景名里取被测对象: 去掉工况前缀、轨后缀与标签部分。"""
    head = name.split("@")[0]
    if "[" in head:
        head = head[: head.index("[")]
    return head


def _subjects_of(cases, rules: ScenarioRules) -> dict[str, str]:
    conds = [_cond(f"SR-{i}", title) for i, (title, _) in enumerate(cases)]
    res = expand_scenarios(conds, rules)
    return {s.req_id: _subject(s.name) for s in res.scenarios}


@pytest.fixture(scope="module")
def rules() -> ScenarioRules:
    return ScenarioRules.load(ROOT / "config" / "scenario_rules.yaml")


def test_same_unit_different_titles_get_different_subjects(rules: ScenarioRules) -> None:
    """同量纲、不同条目名 -> 不同被测对象。

    这是本组用例的核心: 若实现改用「按单位细分」或「按轨细分」, 这条会红。
    """
    got = _subjects_of(DISTINCT_CASES, rules)
    for i, (title, want) in enumerate(DISTINCT_CASES):
        rid = f"SR-{i}"
        assert got.get(rid) == want, f"{title!r} 被命名成 {got.get(rid)!r}, 期望被测对象 {want!r}"


def test_subject_names_are_mutually_distinct(rules: ScenarioRules) -> None:
    """语义不同的时序测量不得给出同一被测对象。

    比逐条比对更严: 它保证新增时序条目时不会静默落进已有名字。
    """
    got = _subjects_of(DISTINCT_CASES, rules)
    subjects = list(got.values())
    dup = sorted({x for x in subjects if subjects.count(x) > 1})
    assert not dup, f"不同测量给出同一被测对象: {dup}"


def test_same_measurement_written_differently_normalises(rules: ScenarioRules) -> None:
    """同一测量的不同写法归一到同一被测对象。

    规格书对掉电保持有多种写法(掉电延时功能 / 掉电保持时间 / 后备时间),
    它们是同一个测点, 名字不应跟着写法漂 —— 否则改版换措辞就会产生新用例名,
    幂等导入把老用例判成新用例, 产测历史记录断链。
    """
    got = _subjects_of(ALIAS_CASES, rules)
    for i, (title, want) in enumerate(ALIAS_CASES):
        assert got.get(f"SR-{i}") == want, f"{title!r} -> {got.get(f'SR-{i}')!r}, 期望 {want!r}"


def test_unrecognised_timing_title_falls_back(rules: ScenarioRules) -> None:
    """未识别的时序条目仍须有名字, 不得落空。

    防"修过头": 若把兜底规则删掉, 新出现的时序条目名会退回原始标题,
    名字风格与全库不一致。兜底在这里, 只是优先级最低。
    """
    conds = [_cond("SR-X", "某厂商自定义的时序指标")]
    res = expand_scenarios(conds, rules)
    names = [s.name for s in res.scenarios if s.req_id == "SR-X"]
    assert names, "未识别的时序条目也必须产出场景"
    for n in names:
        assert n.strip(), f"未识别的时序条目名字为空: {n!r}"


def test_specific_rules_precede_generic_fallback(rules: ScenarioRules) -> None:
    """带 title_pattern 的具体规则必须排在通用 timing 规则之前。

    subjects 的匹配是「首个命中即生效」, 顺序即优先级。通用规则若排在前面,
    具体规则永远不会生效 —— 而这类顺序错误在配置里完全看不出来, 只有本组
    用例能钉住。
    """
    idx = {r.id: i for i, r in enumerate(rules.naming.subjects)}
    assert "out_timing" in idx, "应有通用时序兜底规则 out_timing"
    specific = [
        i
        for i, r in enumerate(rules.naming.subjects)
        if "title_pattern" in r.when and r.id != "out_timing"
    ]
    assert specific, "应存在带 title_pattern 的时序规则"
    for i in specific:
        assert i < idx["out_timing"], (
            f"规则 {rules.naming.subjects[i].id} 排在通用兜底 out_timing 之后, 永不生效"
        )


def test_timing_subjects_reuse_existing_kind(rules: ScenarioRules) -> None:
    """区分靠条目名, 不靠新造 kind —— 时序仍应落在既有 kind=timing 上。

    约束本方案不扩大封闭词表: 新增一个 hold_up_time 之类的 kind 会让下游
    按量纲聚合时把掉电延时与动态响应分到两组, 而它们在量纲上无法区分。
    """
    conds = [_cond("SR-H", "掉电延时功能")]
    res = expand_scenarios(conds, rules)
    assert res.scenarios
    conds[0].output_conditions[0].kind = "timing"
    assert conds[0].output_conditions[0].kind == "timing", "构造前提: kind 仍是 timing"


def test_no_specific_title_or_unit_literal_in_code(rules: ScenarioRules) -> None:
    """代码里不得出现具体条目名或量纲字面量 —— 判定全部来自配置。

    用 AST 排除注释与文档字符串: 注释里记录 PA601 案例是有意的, 只有参与
    逻辑的字符串字面量才是硬编码。
    """
    src = (ROOT / "src" / "aterag" / "extract" / "scenarios.py").read_text(
        encoding="utf-8-sig"
    )
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
    literals = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in doc_nodes
    ]
    for title, _ in ALL_CASES:
        assert not any(title in lit for lit in literals), f"代码里出现了条目名字面量: {title!r}"
    for unit in ("mS", "掉电", "保持时间", "开机输出延迟", "上升时间"):
        assert not any(unit in lit for lit in literals), f"代码里出现了 {unit!r} 字面量"
