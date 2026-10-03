"""L7 文档与门禁层 (W7 交付)。

计划模块 (V6.0 §18.1.3):
    ddl.py        全量 DDL 汇编 (§18.5) —— **已实现**
    registry.py   公式总表生成 (§18.6 步骤 1~7) —— 未实现
    trace.py      追溯矩阵 (§18.6 步骤 17) —— 未实现
    standards.py  标准索引 (§18.6 步骤 3) —— 未实现
    validate.py   G1~G10 质量门禁 (§18.9) —— 未实现

``ddl.py`` 提前于 W7 实现, 是因为 W0 出口判据之一是「两个型号的 schema
建库成功」, 而建库需要一个可 psql 直跑的交付物 (§18.5)。它只做编排:
第 1 分区取 alembic 离线输出, 第 2~4 分区取 ``storage.model_schema``,
都不重新实现 DDL。

门禁是本层的核心价值。生成器只是把手工维护的 CSV 变成 SQL; 门禁才是
拦住「量纲不齐的公式进了库」「规则的 formula_ref 悬空」的机制。
§18.10 注 5 指出, 方案自身的失败模式就是「有生成器但无门禁」。
"""

from __future__ import annotations

from .ddl import SchemaFull, assemble, render, write

__all__ = ["SchemaFull", "assemble", "render", "write"]
