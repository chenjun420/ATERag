"""型号知识从 PostgreSQL 读出, 交给 :mod:`aterag.kg.materialize` 物化。

**两类知识, 两个权威, 互不重叠**
--------------------------------
======================  ==========================  =====================
知识                    权威                        为什么不重叠
======================  ==========================  =====================
领域/通用 (概念/公式/   ``data/seed/power_domain_    种子是 git 内版本化的
标准/公理/定理/符号/    seed.json``(1136 条)        构建产物, 由
负载条件)                                          ``build_seed_data.py``
                                                  可复现; **不灌 PG** ——
                                                  板卡 ``l0_term`` 那 102 条
                                                  公式副本已确认为死数据并
                                                  删除(无消费者、且是种子的
                                                  真子集)
型号 (需求/保护/信号/   PostgreSQL                  抽取产物, 随规格书变化;
分块)                   ``public.aterag_entities``  不是版本化构建产物
                       / ``public.aterag_chunks``
======================  ==========================  =====================

「种子不进 PG」是**删除副本**而不是「换一个副本」: 领域知识一旦落进
``l0_term``, 那些表带 CHECK 约束与量纲校验(``cardinality(dimension_vec)=7``、
``dimension_ok`` 触发器), 而种子的 9 个 entity_type 里有 5 类
(axiom/theorem/symbol/erratum/load_condition)在现有四张表里没有归宿 ——
要灌进去得先写一套带语义判断的映射, 于是又开一个可漂移的写入路径。

**为什么实体读 props 而不是拆列**
--------------------------------
``aterag_entities.props`` 是 JSONB, 里面带 ``section_path`` / ``req_id`` /
``min`` / ``max`` / ``rail`` / ``priority`` 等抽取时的原始字段。这些字段的
集合随 ``config/table_schemas.yaml`` 变化 —— 拆成固定的 PG 列就得跟着改表,
而 JSONB 已经是上游写出来的形态, 直接透传可保证图谱里看到的与抽取结果
**逐字段一致**(可对照, 无中间映射)。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

#: 图谱节点的类型前缀。PG 侧的 etype 与种子侧的 entity_type 是两套词汇,
#: 用前缀而不是裸 etype 是为了让 Explorer 里一眼看出「这条来自型号库还是
#: 领域知识」—— 两者权威不同, 可追溯性要求能区分。
MODEL_ENTITY_PREFIX = "model"

#: 连接 PG 的超时(秒)。**必须有**: 实测不设时, 库不可达(地址写错/板卡离线)
#: 的连接要挂 **132 秒**才失败 —— 而这条路径在 ``analyze_graph`` /
#: ``trace_dependency`` 的同步调用里, 一次分析就把调用方卡住两分钟。
#: 降级只要知道「读不到」就够了, 等满 132 秒没有额外信息量。
PG_CONNECT_TIMEOUT = 5


def _node_id(model_id: str, etype: str, eid: str) -> str:
    """节点 id = **eid 本身**。

    刻意不拼 ``model_id/etype`` 前缀: 抽取层产出的 eid 已经自带型号信息 ——
    实测形态 ``PA601-D54A:-54VRTN@P1`` / ``SR-PA601-D54A-0100@表`` /
    ``PA601-D54A:输出短路保护#-54V``, 再套一层就是
    ``PA601-D54A/Signal/PA601-D54A:-54VRTN@P1``, 型号出现两次, 而 Explorer
    的 URL / 搜索框 / 引用里都要出现这个 id。

    跨型号唯一性不靠「前缀保证」, 而是靠 :func:`iter_model_records` 里的
    **显式冲突检测** —— 真的撞了就抛错, 而不是让两个型号的同名实体在图里
    静默合并成一个节点。后者是知识层最坏的一种失败: 界面看起来正常, 但
    节点属性来自其中一个型号, 另一个型号的知识被吞掉且不可见。
    """
    return eid


def _check_id_collisions(rows: list[tuple[str, str, str]]) -> None:
    """``(model_id, etype, eid)`` 里 eid 撞了就抛。"""
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for model_id, etype, eid in rows:
        prev = seen.get(eid)
        if prev is not None and prev != model_id:
            clashes.append(f"{eid!r} 同时属于 {prev} 与 {model_id}")
        seen[eid] = model_id
    if clashes:
        raise ValueError(
            f"型号知识的 eid 跨型号冲突 {len(clashes)} 处, 无法直接作图谱节点 id "
            f"(会静默合并两个型号的知识): " + "; ".join(clashes[:5])
        )


def iter_model_records(
    dsn: str,
    *,
    model_ids: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """从 ``public.aterag_entities`` 读型号知识, 产出与种子同形的记录。

    产出形状与 ``data/seed/power_domain_seed.json`` 的 ``records`` **完全一致**
    (``id`` 是实体 / ``source_id``+``target_id`` 是关系), 这样
    :func:`aterag.kg.materialize.build_context_graph` 一份代码吃两种来源,
    不需要中间格式 —— 也是「同一份 JSON 能喂三个消费者」这个设计的延续。

    关系: 每个型号一个 Product 节点, 其余实体用 ``has`` 边挂下, 与
    :func:`aterag.kg.entities.entities_to_custom_kg` 同构(同一套本体形状)。
    """
    import psycopg

    sql = "SELECT model_id, etype, eid, props FROM public.aterag_entities"
    params: list[Any] = []
    if model_ids:
        sql += " WHERE model_id = ANY(%s)"
        params.append(model_ids)
    sql += " ORDER BY model_id, etype, eid"

    with psycopg.connect(dsn, connect_timeout=PG_CONNECT_TIMEOUT) as conn:
        rows = conn.execute(sql, params).fetchall()

    _check_id_collisions([(str(m), str(t), str(e)) for m, t, e, _ in rows])

    # 先把实体收齐, 再统一产边 —— 边需要知道该型号有哪些实体, 而 SQL 顺序
    # 不保证 Product 行先到。
    by_model: dict[str, list[tuple[str, str, dict]]] = {}
    for model_id, etype, eid, props in rows:
        by_model.setdefault(str(model_id), []).append((str(etype), str(eid), dict(props or {})))

    for model_id, items in by_model.items():
        product_id = _node_id(model_id, "Product", model_id)
        # 抽取层本身会产出一个 Product 实体(实测 ``etype='Product'``,
        # ``eid=model_id``), 所以这里**不能**再合成一个 —— 同一个 id 进两次
        # add_nodes, 后者覆盖前者, 而两次的 content/metadata 不同, 结果是
        # 「属性来自谁」取决于遍历顺序。只有抽取层没产出 Product 时才补。
        has_product = any(etype == "Product" for etype, _eid, _p in items)
        if not has_product:
            yield {
                "id": product_id,
                "name": model_id,
                "entity_type": f"{MODEL_ENTITY_PREFIX}/Product",
                "text": f"型号 {model_id}",
                "source": f"spec:{model_id}",
                # 字段集与下面从 PG 读出的实体保持一致。合成节点缺
                # ``section``/``metadata`` 会让下游任何按固定字段集读节点的
                # 代码(而不是 ``.get``)在**只有合成节点**的型号上炸掉。
                "section": None,
                "metadata": {
                    "model_id": model_id,
                    "etype": "Product",
                    "authority_kind": "spec",
                },
            }
        for etype, eid, props in items:
            yield {
                "id": _node_id(model_id, etype, eid),
                "name": eid,
                "entity_type": f"{MODEL_ENTITY_PREFIX}/{etype}",
                # content 用 eid + 中文标签: Explorer 的搜索是子串匹配
                # (GraphSession._node_matches_search), 只放 eid 的话
                # 「输出过流」这类中文查询命中不了。
                "text": _label_of(eid, props),
                # 出处是**规格书本身**(型号 + 文档), 章节单独放 section ——
                # 领域知识那边 source 存的是标准号, 两侧语义对齐: 都是
                # 「这条知识从哪份权威文件来」。
                "source": f"spec:{model_id}",
                "section": props.get("section_path") or None,
                "metadata": {
                    "model_id": model_id,
                    "etype": etype,
                    "req_id": props.get("req_id"),
                    "authority_kind": "spec",
                    **{
                        k: v
                        for k, v in props.items()
                        if k
                        in (
                            "min",
                            "max",
                            "typ",
                            "unit",
                            "rail",
                            "priority",
                            "category",
                            "direction",
                            "trip_min",
                            "trip_max",
                        )
                    },
                },
            }
        for etype, eid, _props in items:
            if etype == "Product":
                # Product 自己不挂 has 边(否则是自环)。抽取层已产出 Product 时
                # 节点直接来自那条记录, 不需要这里补节点, 但边要照发。
                continue
            yield {
                "source_id": product_id,
                "target_id": _node_id(model_id, etype, eid),
                "relationship_type": "has",
            }


def _label_of(eid: str, props: dict) -> str:
    """节点的可读文本。优先用抽取出的中文标签, 退化到 eid。"""
    for key in ("label", "title", "zh", "name", "statement", "text"):
        v = props.get(key)
        if isinstance(v, str) and v.strip():
            return f"{eid} {v.strip()}" if not v.startswith(eid) else v.strip()
    return eid


def read_model_records(
    dsn: str,
    *,
    model_ids: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """``iter_model_records`` 的实体/关系拆分形态, 与 ``load_seed_records`` 对齐。"""
    recs = list(iter_model_records(dsn, model_ids=model_ids))
    ents = [r for r in recs if not (r.get("source_id") and r.get("target_id"))]
    rels = [r for r in recs if r.get("source_id") and r.get("target_id")]
    return ents, rels


def graph_stats(dsn: str) -> dict[str, Any]:
    """PG 侧型号知识的规模, 供 /health 与启动日志用。读不到就报「读不到」。

    刻意不吞异常成 ``{}``: 库连不上与「库里没数据」是两件事, 混成前者会让
    「板卡上还没灌知识」看起来像「服务正常但知识为空」。
    """
    import psycopg

    with psycopg.connect(dsn, connect_timeout=PG_CONNECT_TIMEOUT) as conn:
        ent = conn.execute("SELECT count(*) FROM public.aterag_entities").fetchone()[0]
        chunk = conn.execute("SELECT count(*) FROM public.aterag_chunks").fetchone()[0]
        models = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT model_id FROM public.aterag_entities ORDER BY 1"
            ).fetchall()
        ]
    return {"entities": ent, "chunks": chunk, "models": models}


__all__ = [
    "MODEL_ENTITY_PREFIX",
    "graph_stats",
    "iter_model_records",
    "read_model_records",
]
