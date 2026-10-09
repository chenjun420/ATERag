"""负载缩放: Datalog 推出四元组, 这里做乘法 —— **规则刻意不做算术的那个消费方**。

为什么这个模块存在
------------------
``data/seed/power_domain_seed.json`` 里的 ``load-scaling`` 规则是:

    value_at_load(Quantity, Load, FullLoadValue, Ratio) :-
        load_ratio(Load, Ratio), value_at_full_load(Quantity, FullLoadValue).

它**只绑定不缩放**: 头参数第 3 位叫 ``FullLoadValue``, 就是满载基准值本身。
这不是缺陷而是设计, 理由写在 ``build_seed_data.py:95-98``:

    引擎是纯合一, 没有算术。multiply / 内建函数 / 聚合都不存在, 所以
    「30% 载 = 满载 x 0.3」这个乘法在引擎里算不出来。规则只推出
    (量, 工况, 满载基准值, 比例) 四元组, **乘法交给消费方** —— 那样乘法
    步骤本身才是可审计的, 而不是一个藏在引擎里、无法逐步复核的中间值。

实测确认了这条契约: ``DatalogReasoner._parse_rule_string`` 的 body 只接受
``predicate(args)`` 形式的原子(正则 ``([a-zA-Z0-9_]+)\\s*\\(\\s*([^)]+)\\s*\\)``),
所以 ``V = F * R`` 这种算术等式**根本写不进去** —— 强行写会被解析成
「没有 body 原子」而报错。

**缺口**
--------
委托给的那个消费方**从来没被写过**。于是「满载 600W, 半载应该是多少」这个
问题, 规则给出了全部前提(600.0, half_load, 0.5), 却没有任何代码去乘那一下 ——
链断在最后一环。本模块就是补这一环。

**为什么乘法在这里而不在引擎里**
------------------------------
上面那段注释已经给了理由, 且它与本项目的可追溯要求同源: 乘法的**每一步**都
应该能被单独复核。若把缩放塞进推理引擎, 结论就只剩一个中间值, 出错时无法
区分「比例解析错了」与「乘法错了」。这里把两个量都作为返回值交出去:

    ScaledValue(quantity, load, value, ratio, full_load_value)

—— ``value`` 与 ``full_load_value`` 都在, 复核时不必反推。

**不静默的部分**
----------------
``Ratio`` 缺失或非数值时抛错, 不返回 ``None``: 四元组是规则给的, 它算不出来
说明上游出了问题, 而「算不出」与「结果是 0」在返回值上无法区分。

**与 SHACL 的分工**
------------------
本模块**不判断**「半载是否等于满载的一半」。判据在
``domain_rules/power/rules.yaml`` 的 ``constraint.shape`` 段, 由
:class:`aterag.inference.InferenceEngine` 的 ``validate()`` 走 **pyshacl** 执行。
本模块只负责算出候选值, 让约束去判 —— 两个机制各判一次而结论可能相反。

**不是** ``data/seed/power_domain_shapes.ttl``: 那个文件里的
``LoadScalingShape`` 在本仓库**零代码消费者**(只有
``scripts/build_constraints.py`` 生成它、``tests/test_shacl_constraints.py`` 测它),
且它的 7/9 个 NodeShape target ``ex:ModelSpec``, 而种子里没有 model_spec 数据,
所以即便跑起来对真实数据也恒不触发。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScaledValue:
    """一次负载缩放的结果。两个基准值都留着, 便于逐步复核。"""

    quantity: str
    load: str
    value: float
    ratio: float
    full_load_value: float

    def as_fact(self) -> str:
        """给 SHACL / 报告用的三元组形态。"""
        return (
            f"{self.quantity}@{self.load}={self.value} (满载 {self.full_load_value} x {self.ratio})"
        )


def scale(full_load_value: Any, ratio: Any, *, quantity: str, load: str) -> ScaledValue:
    """``value = FullLoadValue x Ratio``。

    ``ratio`` 与 ``full_load_value`` 都接受字符串(``add_fact`` 的字符串分支
    不做类型转换, 推出来的量都是字符串), 所以这里显式转 float 并在失败时
    抛错 —— 静默返回 0.0 会让「比例没解析出来」看起来像「这一档是 0」。
    """
    try:
        ratio_f = float(ratio)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"负载比例无法转成数值: ratio={ratio!r} (quantity={quantity}, load={load})。"
            f"比例来自 load_ratio 规则的四元组, 走到这里说明上游给的不是数值。"
        ) from exc
    try:
        base_f = float(full_load_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"满载基准值无法转成数值: full_load_value={full_load_value!r} "
            f"(quantity={quantity}, load={load})。"
        ) from exc

    return ScaledValue(
        quantity=quantity,
        load=load,
        value=base_f * ratio_f,
        ratio=ratio_f,
        full_load_value=base_f,
    )


def scale_bindings(
    bindings: list[dict[str, Any]],
    *,
    quantity: str,
    value_key: str = "value",
    ratio_key: str = "ratio",
    load_key: str = "load",
    full_load_key: str | None = None,
) -> list[ScaledValue]:
    """把 ``DatalogReasoner.query()`` 的绑定列表缩放成候选值列表。

    ``full_load_key`` 为 None 时用 ``value_key`` 的原值当基准 —— 也就是直接
    乘 ``value x ratio``。对 ``load-scaling`` 那条规则, ``value`` 本身就是
    满载基准值, 所以这样传就对了。**不要**改成「先除后乘」那种看似等价的
    写法: 那样就丢掉了「规则给的是基准值」这个事实, 复核时看不出数据来自哪。
    """
    out: list[ScaledValue] = []
    for b in bindings:
        base = b[value_key] if full_load_key is None else b[full_load_key]
        out.append(
            scale(
                base,
                b[ratio_key],
                quantity=quantity,
                load=str(b[load_key]),
            )
        )
    return out


__all__ = ["ScaledValue", "scale", "scale_bindings"]
