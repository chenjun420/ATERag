"""L1 检索层。

规划 (V6.0 §18.1.3):
    embed.py    嵌入维度探针与维护 (ADR-013)
    graph.py    AGE 图遍历, 按 model_key 分区 (§5.9.1 ·附录)
    hybrid.py   pgvector + BM25 + RRF, 替换 Qdrant 主路 (ADR-014)
    formula.py  公式解析, dimension_ok=false 全拦截
    router.py   ROUTE_RULES 静态路由

状态: hybrid.py 已实现 (ADR-014 W0); 其余四个仍未实现。

**关于 LightRAG mix**: mix 模式**不含 BM25** —— 它是 entities VDB +
relationships VDB + chunks VDB 三次向量检索做 round-robin 合并
(见 lightrag/operate.py)。所以 chunk 层的 BM25 必须自己留着:
中文规格书的精确标识符(SR-1203 / -54V / 11.1A)靠向量命不中。
"""

from __future__ import annotations

__all__: list[str] = ["hybrid"]
