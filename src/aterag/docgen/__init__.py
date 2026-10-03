"""L7 文档与门禁层 (W7 交付)。

计划模块 (V6.0 §18.1.3):
    registry.py   公式总表生成 (§18.6 步骤 1~7)
    trace.py      追溯矩阵 (§18.6 步骤 17)
    standards.py  标准索引 (§18.6 步骤 3)
    ddl.py        全量 DDL 汇编 (§18.5)
    validate.py   G1~G10 质量门禁 (§18.9)

状态: 未实现。

门禁是本层的核心价值。生成器只是把手工维护的 CSV 变成 SQL; 门禁才是
拦住「量纲不齐的公式进了库」「规则的 formula_ref 悬空」的机制。
§18.10 注 5 指出, 方案自身的失败模式就是「有生成器但无门禁」。
"""

from __future__ import annotations

__all__: list[str] = []
