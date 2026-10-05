"""PG -> Semantica ContextGraph 物化: 让 Explorer UI 有数据可看。

**为什么是「每次启动重建」而不是「物化落盘」**
------------------------------------------------
落盘会引入第二份数据副本, 于是知识更新后必须记得同步, 漏一次就出现
「界面显示旧知识、推理用新知识」。这类漂移在本项目已发生过一次(板卡上
``.env`` 写 ``.24`` 而真机是 ``.25``, 代码注释对而配置错), 症状都是
**不报错, 只在用到那条路径时才炸**。所以本层刻意不落盘: 数据唯一来源是 PG,
进程启动时现读现建, 图只活在内存里。

代价是每次启动重建(实测规模: 种子 1136 条 + 型号实体/分块, 秒级), 对一个
交互式 UI 服务可以接受。

**Semantica 的存储限制(已核实, 不是猜测)**
------------------------------------------
* ``explorer/session.py`` 的 ``SUPPORTED_VECTOR_BACKENDS = ("inmemory",
  "sqlite")`` —— 显式拒绝 pgvector/faiss, 因为它们没有 metadata-scoped delete,
  ``update_document`` / ``remove_document`` 无法实现。所以语义检索走不了 PG
  向量, 只能用图遍历 + 内存向量。
* ``GraphSession.from_file`` 是唯一的加载入口, 而 ``create_app(session=...)``
  接受注入的 GraphSession —— 所以**不需要改上游一行代码**, 在自己进程里构造
  ContextGraph 塞进去即可。

**节点/边字段契约**(读 ``context/context_graph.py`` 的 dataclass 确认)
---------------------------------------------------------------------
``ContextNode(node_id, node_type, content, metadata, properties,
valid_from, valid_until)``; ``add_nodes`` 接受 ``{id, type, content,
metadata/properties}``。``ContextEdge(source_id, target_id, edge_type,
edge_id, weight, family_id, metadata, ...)``。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")


def build_context_graph(records: Iterable[Mapping[str, Any]],
                        relationships: Iterable[Mapping[str, Any]],
                        *,
                        advanced_analytics: bool = False):
    """把扁平种子记录构造成 ContextGraph。

    参数形状与 ``data/seed/power_domain_seed.json`` 的 ``records`` 一致 ——
    实体与关系在**同一个数组**里, 靠键区分(``id`` 是实体; ``source_id`` +
    ``target_id`` 是关系)。这正是 ``build_seed_data.to_seed_records`` 产出的
    形状, 也是 ``SeedDataManager.create_foundation_graph`` 读形状, 所以同一份
    JSON 能同时喂给三者, 不需要中间格式转换。
    """
    from semantica.context import ContextGraph

    graph = ContextGraph(advanced_analytics=advanced_analytics)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for rec in records:
        src = rec.get("source_id")
        tgt = rec.get("target_id")
        if src and tgt:
            edges.append(
                {
                    "source_id": str(src),
                    "target_id": str(tgt),
                    "edge_type": str(rec.get("relationship_type") or "related_to"),
                    "metadata": _provenance_of(rec),
                }
            )
            continue
        node_id = rec.get("id")
        if not node_id:
            continue
        nodes.append(
            {
                "id": str(node_id),
                "type": str(rec.get("entity_type") or "entity"),
                "content": str(rec.get("text") or rec.get("name") or node_id),
                "metadata": {
                    **_provenance_of(rec),
                    "authority_kind": (rec.get("metadata") or {}).get(
                        "authority_kind", rec.get("authority_kind")
                    ),
                },
            }
        )

    graph.add_nodes(nodes)
    graph.add_edges(edges)
    return graph, len(nodes), len(edges)


def _provenance_of(rec: Mapping[str, Any]) -> dict[str, Any]:
    """出处字段。

    种子里的 ``source`` 存的是 authority_ref(标准号或条文), 缺失时是空串 ——
    空串必须原样保留而不是省略: 「明确没有出处」与「出处未知」是两回事,
    前者是事实, 后者是没查, 审计时不能混同。
    """
    out: dict[str, Any] = {
        "source": rec.get("source") or "",
        "section": rec.get("section"),
        "confidence": rec.get("confidence"),
    }
    clause = rec.get("clause")
    if clause:
        out["clause"] = clause
    return out


def load_seed_records(seed_path: str) -> tuple[list[dict], list[dict]]:
    """读种子 JSON, 拆成 (实体, 关系)。"""
    from pathlib import Path

    data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    records = data.get("records") or []
    ents = [r for r in records if not (r.get("source_id") and r.get("target_id"))]
    rels = [r for r in records if r.get("source_id") and r.get("target_id")]
    return ents, rels
