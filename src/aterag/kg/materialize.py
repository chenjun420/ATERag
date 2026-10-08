"""PG -> Semantica ContextGraph 物化: 让 Explorer UI 有数据可看。

**为什么是「每次启动重建」而不是「物化落盘」**
------------------------------------------------
落盘会引入第二份数据副本, 于是知识更新后必须记得同步, 漏一次就出现
「界面显示旧知识、推理用新知识」。这类漂移在本项目已发生过一次(板卡上
``.env`` 写 ``.24`` 而真机是 ``.25``, 代码注释对而配置错), 症状都是
**不报错, 只在用到那条路径时才炸**。所以本层刻意不落盘: 数据唯一来源是 PG,
进程启动时现读现建, 图只活在内存里。

代价是每次启动重建(实测规模: 种子 869 条记录 / 594 实体, 加型号实体/分块, 秒级),
对一个交互式 UI 服务可以接受。(早先写的「种子 1136 条」是 2026-09 的旧种子。)

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
import logging
import sys
from collections.abc import Iterable, Mapping
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

logger = logging.getLogger(__name__)


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

    **两类关系记录不建成本地边**
    ----------------------------
    1. ``external: true`` 的记录 —— **走真实种子时一条都不会到这一层**。
       生成器 :func:`build_seed_data.build_relationships` 确实会为带 ``qudt_ref``
       的概念产出 ``has_unit_kind`` -> ``qudt:*`` 并标 ``external: True``, 但
       :func:`build_seed_data.prune_non_executable` 的**双端存活过滤**紧接着把
       它们整条丢掉 —— ``qudt:*`` 不在实体集里(它本来就是 IRI, 不是本地 id)。
       该过滤的注释自己写着「既有 ``qudt:`` 边也是这么消失的」。

       所以这一段是**给绕过生成器的直接调用方兜底的安全网**, 不是运行期主路径。
       早先这里写的是「实测 24 条」并指向 ``build_seed_data.py:930``, 那两个说法
       都不成立了: 条数在变(随概念增删), 而行号早已漂移 —— 照着注释去核对会
       核不到任何东西, 这正是 :class:`TestSeedWiringForExternalRefs` 要挡的。

       语义本身仍必须守住(兜底有意义): ``qudt:PotentialDifference`` 是 IRI 引用,
       库里没有也不该有对应节点, 建边就会得到指向虚空的边, Explorer 里点开是空页。
       到这一层时折进**源节点的 metadata** (``external_refs``), 图内部保持自洽。

    2. ``source_id == target_id`` 的自环(实测 10 条 ``std::X defined_by std::X``,
       ``clause`` 就是标准号本身)。一条 ``defined_by`` 指向自己不含任何信息 ——
       那是 ``standards_add`` 逐条发关系时落下的产物, 不是语义。丢弃并计数。

    目标不在节点集里的边同样丢弃并计数: 留着会让 Explorer 的邻接查询返回
    一个点开没有任何信息的节点, 比没有这条边更糟。
    """
    from semantica.context import ContextGraph

    graph = ContextGraph(advanced_analytics=advanced_analytics)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    # 源节点 id -> {外部 IRI: 本体名}。要在 add_nodes 之后合并, 因为关系记录
    # 可能先于对应实体出现(种子的 records 数组不保证实体在前)。
    external_refs: dict[str, dict[str, str]] = {}
    n_external = n_selfloop = n_dangling = 0
    node_ids: set[str] = set()

    for rec in records:
        src = rec.get("source_id")
        tgt = rec.get("target_id")
        if src and tgt:
            if rec.get("external") is True:
                n_external += 1
                external_refs.setdefault(str(src), {})[str(tgt)] = str(
                    rec.get("ontology") or ""
                )
                continue
            if str(src) == str(tgt):
                n_selfloop += 1
                continue
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
        nid = str(node_id)
        node_ids.add(nid)
        nodes.append(
            {
                "id": nid,
                "type": str(rec.get("entity_type") or "entity"),
                "content": str(rec.get("text") or rec.get("name") or nid),
                "metadata": {
                    **_provenance_of(rec),
                    "authority_kind": (rec.get("metadata") or {}).get(
                        "authority_kind", rec.get("authority_kind")
                    ),
                },
            }
        )

    kept: list[dict[str, Any]] = []
    for e in edges:
        if e["source_id"] in node_ids and e["target_id"] in node_ids:
            kept.append(e)
        else:
            n_dangling += 1
    if n_external or n_selfloop or n_dangling:
        logger.info(
            "关系记录取舍: 外部本体引用 %d 条(折进节点 metadata, 不建边) / "
            "自环 %d 条(丢弃) / 目标不存在 %d 条(丢弃)",
            n_external, n_selfloop, n_dangling,
        )

    for n in nodes:
        refs = external_refs.get(n["id"])
        if refs:
            n["metadata"]["external_refs"] = refs

    graph.add_nodes(nodes)
    graph.add_edges(kept)
    # 返回**图里实际的**数量, 不是传进来的数量。两者会不等: ContextGraph 按
    # (source, target) 去重边(实测种子有 1 组重复的 ``A-4 -> thm::T3``),
    # 而重复节点 id 也只留一个。返回传入值会让调用方以为「传了 76 条就有
    # 76 条边」, 于是 76 与 75 的差成了无法解释的谜团 —— 而这正是本项目
    # 反复吃亏的那类「不报错的偏差」。
    return graph, len(graph.nodes), len(graph.edges)


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
