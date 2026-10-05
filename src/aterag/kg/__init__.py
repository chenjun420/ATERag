"""知识图谱层: 规格书实体 -> 本体节点与关系, 再合成 Semantica ContextGraph。

四个模块的分工:

* :mod:`aterag.kg.entities`  —— 实体到本体节点/关系的映射(摄取期用)
* :mod:`aterag.kg.materialize` —— 扁平记录 -> ``ContextGraph``(纯内存, 不落盘)
* :mod:`aterag.kg.pg_source` —— 型号知识从 PostgreSQL 读出
* :mod:`aterag.kg.graph`     —— 两类知识合成一张图, 供 Explorer 消费
"""

from __future__ import annotations

__all__ = ["entities", "graph", "materialize", "pg_source"]
