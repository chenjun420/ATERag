"""L2 确定性推理层: **已落地的只有工况缩放**, 其余是记在案的未做决定。

**别被旧版说明骗了** —— 上一版这里写着「状态: 未实现」, 而包内其实已经
有能跑且有测试的 :mod:`aterag.reasoning.load_scaling`。一个模块同时挂着
「未实现」和被 ``tests/test_load_scaling.py`` 钉住的行为, 是最坏的状态:
没人知道该信哪句。

已实现
------
:mod:`aterag.reasoning.load_scaling`
    工况比例 -> 量值缩放。Datalog 只推出 (量, 工况, 满载基准值, 比例)
    四元组, 乘法由这个模块做, 于是乘法那一步本身可审计。

未做, 且是**决定**不是遗漏
--------------------------
V6.0 §18.1.3 列的 Datalog 层(事实/规则/推理三件套)、SHACL Shape 1~13、
双时态区间判定、递归影响面、Rete 网络、语义图查询。理由按可靠性分三层:

1. **数值推理走不到 Datalog, 也不该走。**
   ``semantica.reasoning.reasoner.Reasoner.add_fact`` 签名是
   ``Union[str, Dict[str, Any]]``, 实现里把 dict **压成字符串**
   (``Type(name)`` / ``RelType(source, target)``), 数值载荷在转换时丢掉;
   不含 ``type``+``name/id`` 也不含 ``source_id``/``target_id`` 的 dict
   **被静默丢弃, 连错都不报**。所以带数值的推理只能走
   :class:`aterag.inference.InferenceEngine` + ``domain_rules/*/rules.yaml``
   —— 规则是声明式的、被自校 (``scripts/rules_selftest.py``)、且每条都带
   出处与 ``confidence``。
2. **Datalog 不是没在用, 而是被限在它能做的事上。**
   :mod:`load_scaling` 的工况比例确实走
   ``semantica.reasoning.datalog_reasoner.DatalogReasoner``(那个类的
   ``add_fact`` 收任意值)。三条实测契约记在
   ``scripts/build_seed_data.py`` 的 ``LOAD_RULES`` 注释里: 变量必须首字母
   大写(否则被当常量, 推导为空且不报错)、同一变量名只能表一件事、**引擎
   是纯合一没有算术**。第三条决定了它只能推四元组。
3. **SHACL 那片已有归属。** 「半载是否等于满载的一半」由
   ``LoadScalingShape`` 判(``data/seed/power_domain_shapes.ttl``),
   :mod:`load_scaling` 刻意**不**判断 —— 两个机制各判一次而结论可能相反。

要动这片之前先读上面三条。第 1 条是上游契约, 升级 semantica 后要重新验。
"""

from __future__ import annotations

from aterag.reasoning.load_scaling import ScaledValue, scale, scale_bindings

__all__ = ["ScaledValue", "scale", "scale_bindings"]
