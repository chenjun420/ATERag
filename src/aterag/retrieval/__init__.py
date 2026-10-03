"""L1 检索层 (W3 交付)。

计划模块 (V6.0 §18.1.3):
    embed.py    嵌入客户端与维度校验 (ADR-013)
    graph.py    AGE 子图, 按 model_key 绑定 (§5.9.1 路由注入点)
    hybrid.py   pgvector + BM25 + RRF, 替换 Qdrant 路径 (ADR-014)
    formula.py  公式卡检索, dimension_ok=false 禁入索引
    router.py   ROUTE_RULES 动态路由

状态: 未实现。
"""

from __future__ import annotations

__all__: list[str] = []
