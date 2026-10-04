"""Datalog 事实 + 规则: **端到端真的能推出结论**。

为什么必须有这个测试
------------------
``add_rule()`` 对任何语法合法的规则都不报错, ``add_fact()`` 对无法识别的
形态也只 ``logger.warning`` 后 ``return``。于是「规则集是空的」这件事**没有任何
报错**: 早先只发布两条规则、零条事实, 校验全过、语法全对, 而 ``derive_all()``
永远为空 —— 整条工况推理链是死的。

更隐蔽的一层: 事实里的常量若写成 ``half load``(空格)而别处写 ``half_load``
(下划线), 规则**永远匹配不上**, 同样不报错。所以光断言「文件里有 facts」不够,
必须真的跑一遍引擎, 断言推出来的结果。

用法: 事实与规则一律**从交付文件读**, 不在本文件里另写一份 —— 另写一份就等于
验证了一份与交付物无关的数据, 而交付物本身可能已经漂了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("semantica.reasoning.datalog_reasoner")

from semantica.reasoning.datalog_reasoner import DatalogReasoner  # noqa: E402

SEED = Path("data/seed/power_domain_seed.json")


@pytest.fixture(scope="module")
def seed() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


def _engine(seed: dict, *, with_rules: bool = True, extra_facts: tuple[str, ...] = ()) -> DatalogReasoner:
    eng = DatalogReasoner()
    for fact in seed["facts"]:
        eng.add_fact(fact["fact_str"])
    for fact in extra_facts:
        eng.add_fact(fact)
    if with_rules:
        for rule in seed["rules"]:
            eng.add_rule(rule["rule_str"])
    return eng


# ---------------------------------------------------------------------------
# 事实确实被发布了 —— 且形态是引擎认得的
# ---------------------------------------------------------------------------


def test_facts_are_published(seed: dict) -> None:
    assert seed["facts"], "交付文件里没有 facts —— 规则集将是死的"


def test_every_fact_parses_as_a_constant(seed: dict) -> None:
    """常量首字母大写会被 ``add_fact`` 直接 ``raise``。

    引擎据此判变量: 事实里出现 ``Load`` 会被当变量, 而事实必须是常量。
    这条在构造时就炸, 所以能用异常断言。
    """
    for fact in seed["facts"]:
        eng = DatalogReasoner()
        eng.add_fact(fact["fact_str"])  # 不抛异常即为常量
        derived = eng.derive_all()
        assert fact["fact_str"].rstrip(".") in derived, f"事实没被收录: {fact['fact_str']}"


def test_load_key_is_normalized_to_underscore(seed: dict) -> None:
    """实体侧与事实侧的工况名必须同形态, 否则规则永远匹配不上且不报错。

    早先实体存 ``half load``(空格)、别名侧用 ``half_load``(下划线), 两边对不上。
    """
    for rec in seed["records"]:
        if rec.get("entity_type") == "load_ratio":
            assert " " not in rec["load"], f"load 字段含空格, 与事实侧不一致: {rec['load']}"
    for fact in seed["facts"]:
        for arg in fact["args"]:
            assert " " not in arg, f"事实实参含空格: {fact['fact_str']}"


# ---------------------------------------------------------------------------
# 端到端: 规则真的在起作用
# ---------------------------------------------------------------------------


def test_alias_is_derived_only_by_the_rule(seed: dict) -> None:
    """``50pct_load`` 只能由 ``load-alias`` 规则推出。

    对照: 去掉规则后必须推不出来 —— 否则「规则在起作用」这个结论是假的。
    """
    with_rules = {r["Load"] for r in _engine(seed).query("load_ratio(Load, Ratio)")}
    without_rules = {
        r["Load"] for r in _engine(seed, with_rules=False).query("load_ratio(Load, Ratio)")
    }

    assert "50pct_load" in with_rules, "别名未归一: 50pct 载与半载应是同一工况"
    assert "50pct_load" not in without_rules, "不加规则也推出别名 -> 规则没起作用"


def test_half_load_ratio(seed: dict) -> None:
    """半载 = 50%载 = 满载 x 50%。"""
    rows = {r["Load"]: r["Ratio"] for r in _engine(seed).query("load_ratio(Load, Ratio)")}
    assert rows["half_load"] == "0.5"
    assert rows["50pct_load"] == "0.5"
    assert rows["full_load"] == "1.0"


def test_scaling_needs_model_data_and_does_not_invent_it(seed: dict) -> None:
    """``value_at_load`` 需要型号功率数据; 没有时必须**推不出**, 而不是编一个值。

    凭空给功率值会让推理在错误型号上运行且不报错, 所以这里断言空结果。
    """
    assert _engine(seed).query("value_at_load(Q, Load, FullLoadValue, Ratio)") == []


def test_scaling_derives_quadruple_once_model_data_present(seed: dict) -> None:
    """补上型号满载功率后推出四元组 (量, 工况, 满载基准值, 比例)。

    引擎无算术, 所以只推比例; ``600.0 x 0.5 = 300.0`` 由消费方算 —— 乘法步骤
    本身才可审计, 而不是藏在引擎里。
    """
    eng = _engine(seed, extra_facts=("value_at_full_load(pout_max, 600.0)",))
    rows = eng.query("value_at_load(Quantity, Load, FullLoadValue, Ratio)")
    half = next((r for r in rows if r["Load"] == "half_load"), None)
    assert half is not None, f"补了满载事实仍推不出半载四元组: {rows}"
    assert half["Quantity"] == "pout_max"
    assert half["FullLoadValue"] == "600.0"
    assert half["Ratio"] == "0.5"


# ---------------------------------------------------------------------------
# 交付文件与源码常量的一致性 —— 防漂移
# ---------------------------------------------------------------------------


def test_facts_cover_every_load_ratio_with_a_value(seed: dict) -> None:
    """每条有比例的工况都必须有对应事实, 规则才可能匹配到它。"""
    from scripts.build_seed_data import LOAD_CONDITIONS, _load_key

    expected = {_load_key(i["en"]) for i in LOAD_CONDITIONS if i["ratio"] is not None}
    published = {f["args"][0] for f in seed["facts"] if f["predicate"] == "load_ratio"}
    assert published == expected


def test_rule_str_matches_what_add_rule_accepts(seed: dict) -> None:
    """``rule_str`` 是给 ``add_rule()`` 的形态(只收字符串)。"""
    for rule in seed["rules"]:
        assert isinstance(rule["rule_str"], str)
        DatalogReasoner().add_rule(rule["rule_str"])  # 语法非法会 raise
