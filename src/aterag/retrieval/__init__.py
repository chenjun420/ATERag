"""L1 检索层。

规划 (V6.0 §18.1.3):
    embed.py    嵌入维度探针与维护 (ADR-013)
    graph.py    AGE 图遍历, 按 model_key 分区 (§5.9.1 ·附录)
    hybrid.py   pgvector + BM25 + RRF, 替换 Qdrant 主路 (ADR-014)
    formula.py  公式解析, dimension_ok=false 全拦截
    router.py   ROUTE_RULES 静态路由

状态: hybrid.py 已实现, 且是**唯一的检索实现** (ADR-014 W0 + LightRAG 移除);
其余四个仍未实现。graph.py 待 Semantica 图谱就位后实现。

**BM25 那一路为什么必须自己留着**: 中文规格书的精确标识符(SR-1203 / -54V /
11.1A)靠向量命不中。LightRAG 的 mix 模式实测不含 BM25(决策依据存档见
``hybrid.py`` 的模块 docstring), 已随该依赖一并移除。
"""

from __future__ import annotations

__all__: list[str] = ["hybrid"]
