"""``load_scaling`` 的测试 —— 钉住「规则不缩放、消费方缩放」这个分工。

分工的依据写在 ``build_seed_data.py:95-98``: Datalog 引擎是纯合一、没有算术,
所以规则只推出四元组, 乘法由消费方做(为了乘法步骤本身可审计)。这些用例防的
是**有人把乘法塞回规则**或**把消费方当成规则已经做完**。
"""

from __future__ import annotations

import pytest

from aterag.reasoning.load_scaling import ScaledValue, scale, scale_bindings


class TestScale:
    def test_multiplies_base_by_ratio(self) -> None:
        r = scale("600.0", "0.5", quantity="pout_max", load="half_load")
        assert r.value == 300.0
        assert r.ratio == 0.5
        assert r.full_load_value == 600.0
        assert r.quantity == "pout_max"
        assert r.load == "half_load"

    def test_keeps_full_load_value_for_audit(self) -> None:
        """两个值都要在返回值里。

        只回 ``value`` 的话, 复核时要反推「基准是多少」—— 而反推需要相信
        ``value / ratio`` 这步除法本身没错, 那就等于用一个未经校验的运算去
        校验另一个运算。
        """
        r = scale(600.0, 0.5, quantity="q", load="half_load")
        assert r.full_load_value == 600.0
        assert r.value == pytest.approx(r.full_load_value * r.ratio)

    def test_accepts_strings_from_datalog(self) -> None:
        """``add_fact`` 的字符串分支不做类型转换, 推出来的量全是字符串。"""
        r = scale("600.0", "0.5", quantity="q", load="half_load")
        assert r.value == 300.0

    def test_full_load_identity(self) -> None:
        r = scale(600.0, 1.0, quantity="q", load="full_load")
        assert r.value == 600.0

    def test_no_load_is_zero(self) -> None:
        r = scale(600.0, 0.0, quantity="q", load="no_load")
        assert r.value == 0.0

    def test_agrees_with_shacl_load_scaling_shape(self) -> None:
        """与 ``LoadScalingShape`` 的判据一致: 半载 = 满载 / 2。

        shape 文件原文: 「半载功率必须等于满载功率的一半」。
        """
        r = scale(600.0, 0.5, quantity="pout_max", load="half_load")
        assert r.value == r.full_load_value / 2


class TestScaleRejectsGarbage:
    def test_non_numeric_ratio_raises(self) -> None:
        """比例拿不到就抛, 不返回 0.0。

        静默返回 0 会让「比例没解析出来」看起来像「这一档功率是 0」——
        而 0 是一个物理上可能的答案, 于是错误不可见。
        """
        with pytest.raises(ValueError, match="负载比例无法转成数值"):
            scale(600.0, "half", quantity="q", load="half_load")

    def test_non_numeric_base_raises(self) -> None:
        with pytest.raises(ValueError, match="满载基准值无法转成数值"):
            scale("满载", "0.5", quantity="q", load="half_load")

    def test_none_ratio_raises(self) -> None:
        with pytest.raises(ValueError):
            scale(600.0, None, quantity="q", load="half_load")


class TestScaleBindings:
    def test_scales_every_binding(self) -> None:
        rows = [
            {"load": "full_load", "value": "600.0", "ratio": "1.0"},
            {"load": "half_load", "value": "600.0", "ratio": "0.5"},
            {"load": "no_load", "value": "600.0", "ratio": "0.0"},
        ]
        out = scale_bindings(rows, quantity="pout_max")
        assert [o.value for o in out] == [600.0, 300.0, 0.0]
        assert all(o.quantity == "pout_max" for o in out)

    def test_explicit_full_load_key(self) -> None:
        """给了 ``full_load_key`` 就以它为基准, 而不是拿 value 再乘。"""
        rows = [{"load": "half_load", "value": "300.0", "ratio": "0.5", "full_load": "600.0"}]
        out = scale_bindings(rows, quantity="p", full_load_key="full_load")
        assert out[0].value == 300.0
        assert out[0].full_load_value == 600.0

    def test_empty_bindings(self) -> None:
        assert scale_bindings([], quantity="p") == []


def test_datalog_silently_drops_arithmetic_in_rule_bodies() -> None:
    """钉住「为什么乘法不放在规则里」这个前提 —— 而且是**静默**的。

    实测: ``add_rule`` 接受下面这条含算术的规则**且不报错**, 但
    ``_parse_rule_string`` 的 body 正则只提取能匹配的 ``predicate(args)``
    原子, 于是 ``V = F * R`` 被**丢弃**; 随后 ``V`` 成了头变量却没有任何
    body 原子绑定它, ``query`` 返回 ``[]``。

    这是本项目反复遇到的那类失败: 语法校验全过、``add_rule`` 不报错、
    ``derive_all()`` 正常跑完, 只是结果为空。开发者会误判成「规则不工作」,
    而真因是「这门语言里没有算术」。所以本用例断言的是**实际行为**(不抛错 +
    查不出结果), 而不是我们期望的行为 —— 等哪天 Semantica 支持算术了,
    这条会红, 那时该重新评估是把缩放挪进规则还是保留现状。
    """
    from semantica.reasoning.datalog_reasoner import DatalogReasoner

    dr = DatalogReasoner()
    dr.add_rule(
        "value_at_load(Q, L, V, R) :- load_ratio(L, R), value_at_full_load(Q, F), V = F * R."
    )  # 不抛错 —— 这一点本身就是缺陷
    dr.add_fact("load_ratio(half_load, 0.5)")
    dr.add_fact("value_at_full_load(pout_max, 600.0)")
    assert dr.query("value_at_load(pout_max, ?load, ?v, ?r)") == [], (
        "若这条开始返回结果, 说明 Datalog 支持算术了 -> 重新评估缩放该放哪"
    )


def test_shipped_load_scaling_rule_shape_is_still_parseable() -> None:
    """现行规则(不含算术)必须仍能被解析, 且给出的是**基准值 + 比例**。"""
    from semantica.reasoning.datalog_reasoner import DatalogReasoner

    dr = DatalogReasoner()
    dr.add_rule(
        "value_at_load(Quantity, Load, FullLoadValue, Ratio) :- "
        "load_ratio(Load, Ratio), value_at_full_load(Quantity, FullLoadValue)."
    )
    dr.add_fact("load_ratio(half_load, 0.5)")
    dr.add_fact("value_at_full_load(pout_max, 600.0)")
    # 查询里的变量名决定返回字典的键, 所以用 ?value/?ratio 与
    # scale_bindings 的默认键名对上 —— 这也是它的预期用法。
    rows = dr.query("value_at_load(pout_max, ?load, ?value, ?ratio)")
    assert rows == [{"load": "half_load", "value": "600.0", "ratio": "0.5"}], (
        "规则给出的应是**满载基准值 + 比例**, 缩放由消费方做"
    )
    # 缩放之后才是物理答案
    assert scale_bindings(rows, quantity="pout_max")[0].value == 300.0


def test_scalevalue_as_fact_is_readable() -> None:
    r = ScaledValue("pout_max", "half_load", 300.0, 0.5, 600.0)
    assert "300.0" in r.as_fact()
    assert "600.0" in r.as_fact()
