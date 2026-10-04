"""L7 文档与门禁层 (W7 交付)。

已实现模块:
    ddl.py        全量 DDL 汇编 (§18.5)
    spec_parse.py 公式表确定性抽取 (§18.6 步骤 1)
    symbols.py    U.5 符号表 -> (符号, 命名空间) 维度字典
    expr_norm.py  语料表达式 -> 引擎语法归一化
    quantity_rules.py 命名约定规则表(唯一推断来源)
    standards.py  标准索引 (§18.6 步骤 3) —— 177 条
    errata.py     勘误 E-1~E-7 关联 (§18.6 步骤 5) —— 7/7

计划模块 (尚未实现):
    registry.py   公式总表生成 (§18.6 步骤 1~7)
    trace.py      追溯矩阵 (§18.6 步骤 17)
    validate.py   G1~G10 质量门禁 (§18.9)

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
