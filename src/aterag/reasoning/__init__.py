"""L2 确定性推理层: **生产面上一条入口都没有**, 包内代码只被测试与 verify 脚本消费。

**别被旧版说明骗了, 也别被这一版吓到** —— 上一版这里写着「状态: 未实现」,
而包内其实有能跑且有测试的 :mod:`aterag.reasoning.load_scaling`; 现在反过来,
模块确实存在、也确实能跑, 但**没有任何生产调用方**。

真实可达性(实测, 2026-10-08)
---------------------------
* :func:`~aterag.reasoning.load_scaling.scale_bindings` 的调用点只有
  ``tests/test_load_scaling.py`` 与 ``scripts/verify_joint_reasoning.py:315``。
  ``src/`` 里的唯一一处是本文件的 ``__all__`` 再导出。**所以下面「已实现」的
  那段能力, 在服务里跑不到** —— 真正的数值推理走
  :class:`aterag.inference.InferenceEngine` + ``domain_rules/*/rules.yaml``。
* 同理, 「Datalog 已被用在它能做的事上」这句也要打折: 那条路径经由
  ``scale_bindings`` 进入, 而它只在 verify 脚本里被调用。**生产面上 Datalog
  引擎的使用者是零**。

已实现(代码与测试齐备, 但未接生产)
----------------------------------
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
2. **Datalog 的能力边界是真的, 但「已在用」不成立。**
   ``DatalogReasoner`` 的 ``add_fact`` 收任意值, 三条实测契约记在
   ``scripts/build_seed_data.py`` 的 ``LOAD_RULES`` 注释里: 变量必须首字母
   大写(否则被当常量, 推导为空且不报错)、同一变量名只能表一件事、**引擎
   是纯合一没有算术**。第三条决定了它只能推四元组。但 ``load_scaling``
   本身没接生产(见上), 所以这三条目前是知识而不是既成事实。
3. **SHACL 那片另有归属, 但不是本包。** 「半载是否等于满载的一半」由
   ``InferenceEngine.validate`` 走 **pyshacl** 判, 约束体是
   ``domain_rules/power/rules.yaml`` 的 ``constraint.shape`` 段(77 条)。
   **不是** ``data/seed/power_domain_shapes.ttl`` —— 那个文件(9 个
   NodeShape, 7 个 target ``ex:ModelSpec``)在本仓库**零代码消费者**,
   ``src/`` 里只有本文件与 ``load_scaling.py`` 两处注释提到它。
   :mod:`load_scaling` 刻意**不**判断 —— 两个机制各判一次而结论可能相反。

要动这片之前先读上面三条。第 1 条是上游契约, 升级 semantica 后要重新验。
"""

from __future__ import annotations

from aterag.reasoning.load_scaling import ScaledValue, scale, scale_bindings

__all__ = ["ScaledValue", "scale", "scale_bindings"]
