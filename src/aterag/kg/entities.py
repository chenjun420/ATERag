"""规格书实体 -> 知识图谱节点与关系。

描述的是「规格书表格抽出的实体」到「本体节点/关系」的形状: 每个型号一个
Product 节点, 其余实体用 has 边挂在它下面。

返回 ``{"entities": [...], "edges": [...], "triplets": []}``: 前两个键与
Semantica ``SeedDataManager.create_foundation_graph`` 读的 ``entities`` /
``relationships`` 一一对应(关系的 source_id / target_id / relationship_type
会被读成边), ``triplets`` 保留空列表以兼容该结构。
"""

from __future__ import annotations

import json

__all__ = ["entities_to_custom_kg"]


def entities_to_custom_kg(entities: list, model_id: str) -> dict:
    """抽取实体 -> 知识图谱节点与边 (确定性映射, 不调 LLM)。"""

    def node(e):
        return {
            "id": f"{e.etype}:{e.eid}",
            "entity_type": e.etype,
            "entity_name": e.eid,
            "content": json.dumps(e.props, ensure_ascii=False),
            "source_id": e.props.get("req_id", "") or e.eid,
            "metadata": {
                "section_path": e.props.get("section_path", ""),
                "model_id": e.props.get("model_id", model_id),
            },
        }

    nodes = [node(e) for e in entities]
    edges = []
    for e in entities:
        if e.etype == "Product":
            continue
        edges.append(
            {
                "source": f"Product:{model_id}",
                "target": f"{e.etype}:{e.eid}",
                "relationship": "has" if e.etype != "Product" else "self",
                "weight": 1.0,
                "source_id": e.eid,
            }
        )
    return {"entities": nodes, "edges": edges, "triplets": []}
