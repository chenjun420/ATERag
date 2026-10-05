"""L2 确定性推理层 (W2 交付)。

计划模块 (V6.0 §18.1.3):
    datalog.py    事实/规则/推理三件套, 每条 Deduction 必须带非空 proof
    shacl.py      Shape 1~13
    temporal.py   区间包含与相交判定 (双时态的推理侧)
    impact.py     递归影响面
    rete.py       Rete 网络
    sparql.py     语义图查询

状态: 未实现。本文件先落地, 使包结构与 §18.1.3 对齐, 并让 mypy strict
能把该目录纳入检查范围 (ADR-017: 退役一个旧目录开一个, 新层从第一行
代码起就在类型门禁内)。
"""

from __future__ import annotations

from aterag.reasoning.load_scaling import ScaledValue, scale, scale_bindings

__all__ = ["ScaledValue", "scale", "scale_bindings"]
