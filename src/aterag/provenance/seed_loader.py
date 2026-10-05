"""把种子里已有的溯源信息灌进 PG。

**先说清「已有」与「新增」的界线**, 因为这决定了这个模块的价值:

===========================  ==================================  ====================
信息                          在种子里的形态                        灌进库后多了什么
===========================  ==================================  ====================
每条知识的出处                 记录上的 ``source`` / ``clause`` /    可**按 id 查**
                             ``confidence`` / ``authority_kind``    (原来只能全量扫 JSON)
知识之间的推导关系            ``records`` 里的关系记录              进 ``l0_term.trace``,
                             (``source_id`` -> ``target_id``)      可索引、可标未审
相对方案原文的修正            ``provenance.corrections_applied``    每条修正的 before/after
                             (每条带 ``matched``/``before``)        变成可查的版本关系
===========================  ==================================  ====================

也就是说: **出处信息本来就在, 但它是「长在 JSON 上的」而不是「可查询的谱系」**。
这个模块不创造新的出处信息, 它把已有的出处**接进** Semantica 的谱系机制。

两张表, 两种东西
----------------
* ``l0_term.provenance`` —— **逐条事实的出处**。经
  :class:`~aterag.provenance.pg_storage.PGProvenanceStorage` 交给
  ``ProvenanceManager`` 管理, 于是上游的 ``trace_lineage`` /
  ``get_lineage`` / ``verify_chain`` / ``export_prov`` / ``audit_log``
  这些能力第一次真的能用上。
* ``l0_term.trace`` —— **推导边**(谁由谁推出)。

**推导边为什么不写成 provenance 的伪实体**
----------------------------------------
最初是那么做的(``entity_id = "applies_to_formula:A-12->F_N.2_LOSS_BUDGET"``),
结果 ``export_prov("turtle")`` 直接崩:

    Exception: "https://semantica.dev/ns#applies_to_formula:A-12->F_N.2_LOSS_BUDGET"
    does not look like a valid URI, I cannot serialize this as N3/Turtle.

``entity_id`` 会进 IRI 位置(``uri()`` 拼成 ``https://semantica.dev/ns#{id}``),
而推导边的天然形状是「关系 + 两端」, 拼成字符串必然带非法字符。转义能糊过去,
但那是把「边」硬塞进「节点」表。``trace`` 本来就是为推导链建的, 用它形状才
一致 —— 而且顺带白拿一个能力: ``trace.verified=false`` 能标出「这条推导还没
人审」, 门禁 G 系列正是看这个。

**不把 ``external: true`` 的 QUDT 引用写成任何东西**
------------------------------------------------------
那些关系指向外部本体, 不是本地实体。给 ``qudt:PotentialDifference`` 建一条
谱系或一条推导边, 等于给一个不存在的实体建了一条链 —— 而链的价值恰恰在于
「每一环都存在」。它们作为属性存在节点的 ``metadata`` 里(见
:mod:`aterag.kg.materialize`)。

**``ProvenanceManager.export_prov()`` 在本项目的 id 空间下不可用**
----------------------------------------------------------------
它把 ``entity_id`` 拼成 IRI(``https://semantica.dev/ns#{id}``)再交给 rdflib
序列化, 而我们的 id 空间**不是 IRI 安全的** —— 实测会崩在:

    Exception: "https://semantica.dev/ns#correction:std::GB 4943.1-2011"
    does not look like a valid URI, I cannot serialize this as N3/Turtle.

含空格(``std::GB 4943.1-2011``)、含双冒号(``std::`` / ``load::``)是**标准号与
工况名的固有写法**, 不是可以清洗掉的脏数据。所以这里的选择是:

* **不**改 id(改了就与种子、图谱、MCP 返回值三处对不上, 而 id 是全系统的
  连接键);
* **不**做 IRI 转义(转义后 `get_provenance("std::GB 4943.1-2011")` 就查不到,
  除非再写一层反转义, 那是净增复杂度);
* RDF 导出走仓库既有的 :mod:`scripts.build_seed_data` -> ``power_domain.rdf.ttl``
  那条路(它已经能处理这些 id), 而不是 ``export_prov``。

需要按 id 查谱系时用 ``get_provenance`` / ``trace_lineage`` / ``revision_history``,
这三个都不受 IRI 限制。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from semantica.provenance.manager import ProvenanceManager

logger = logging.getLogger(__name__)

#: 灌库活动名。写进每条 entry 的 ``activity_id``, 于是审计能问出
#: 「这条知识是哪一次装载进来的」—— 种子重跑一次就是一个新的 activity。
SEED_ACTIVITY = "seed_load"

#: ``authority_kind`` -> 可信度。冲突消解**只**按 credibility 排
#: (不用 recency / first_seen —— 种子时间戳大量为空, 排了等于没排)。
#: 所以每个值都必须有, 缺失时 ConflictResolver 就没有排序依据。
#:
#: 分档不是凭感觉定的, 是按「这个依据能不能被第三方独立复核」分:
#: 有国标/行标条款号可查(0.9) > 项目自定义可协商(0.5) > 无出处(0.2)。
CREDIBILITY_BY_AUTHORITY: dict[str, float] = {
    "standard": 0.9,
    "industry": 0.6,
    "project_defined": 0.5,
    "unverified": 0.2,
}

#: 谱系表的 schema。领域知识跨型号共享, 所以放 L0(§6.2), 不进型号 schema。
TRACE_SCHEMA = "l0_term"


# ---------------------------------------------------------------- 取值


def _authority_kind_of(record: dict[str, Any]) -> str | None:
    """取 ``authority_kind``。

    **位置有两种**, 这是实测踩到的: 领域知识的记录把它放在顶层
    (``HYSTERESIS`` -> ``authority_kind='standard'``), 而另一些放在
    ``metadata`` 下面 —— :func:`aterag.kg.materialize` 因此写的是
    ``rec["metadata"].get("authority_kind", rec.get("authority_kind"))``。
    只读顶层会让一部分记录静默取不到, 而这里取不到的后果是 credibility 为
    None, 冲突消解就没有排序依据。
    """
    meta = record.get("metadata")
    if isinstance(meta, dict) and meta.get("authority_kind"):
        return str(meta["authority_kind"])
    val = record.get("authority_kind")
    return str(val) if val else None


def _credibility_of(record: dict[str, Any]) -> float | None:
    """出处可信度; ``authority_kind`` 缺失时返回 None。

    None 与 0.0 是两回事: 前者「没评估过」, 后者「评估为最不可信」。
    """
    ak = _authority_kind_of(record)
    return CREDIBILITY_BY_AUTHORITY.get(ak) if ak else None


def _location_of(record: dict[str, Any]) -> str | None:
    """出处位置。``clause`` 是条款号, ``section`` 是章节, 两者都有就都带上。"""
    parts = [p for p in (record.get("clause"), record.get("section")) if p]
    return " / ".join(str(p) for p in parts) if parts else None


# ---------------------------------------------------------------- 记录挑选


def _iter_entity_records(records: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """挑出实体记录(有 ``id`` 的)。

    关系记录(有 ``source_id`` + ``target_id`` 的)单独处理 —— 它们是推导边,
    不是带出处的实体, 塞进 ``track_entity`` 会凭空多出一批实体。
    """
    for rec in records:
        if rec.get("source_id") and rec.get("target_id"):
            continue
        if rec.get("id"):
            yield rec


def _iter_derivation_records(records: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """挑出**本地且有意义**的推导关系。

    排除两类, 与 :func:`aterag.kg.materialize.build_context_graph` 的口径
    **逐条一致** —— 两侧对「什么算一条边」的判断若不同, 就会出现「图里没有
    这条边但 trace 里有这条推导」的矛盾, 而 trace 是审计依据, 审计看到矛盾时
    无法判断该信哪个:

    1. ``external: true`` 的 QUDT 引用(实测 24 条);
    2. 自环(实测 10 条 ``std::X defined_by std::X``)—— 不含信息。
    """
    for rec in records:
        if not (rec.get("source_id") and rec.get("target_id")):
            continue
        if rec.get("external") is True:
            continue
        if str(rec["source_id"]) == str(rec["target_id"]):
            continue
        yield rec


def _corrections_of(seed: dict[str, Any]) -> list[dict[str, Any]]:
    return list((seed.get("provenance") or {}).get("corrections_applied") or [])


# ---------------------------------------------------------------- provenance


def load_seed_provenance(
    manager: ProvenanceManager,
    seed: dict[str, Any],
    *,
    batch_activity: str = SEED_ACTIVITY,
) -> dict[str, int]:
    """把种子文档的**逐条出处**灌进 ``manager``。返回计数, 有失败就抛。

    幂等性: 重复调用会为同一条知识**再写一条** entry(而不是覆盖), 因为
    ``ProvenanceManager`` 的语义是「追加一次溯源事实」——
    「这条知识在 10 月 5 日被装载过, 出处是 X」是历史, 覆盖它等于丢历史。
    看「当前有效的那条」用 ``get_provenance``, 看全部用 ``revision_history``。

    **为什么失败必须抛**
    -------------------
    ``ProvenanceManager.track_entity`` 按上游设计会**吞掉存储异常**: 它
    ``_raise_on_error=True`` 之后仍然 catch 住、``logger.error`` 一下、返回
    ``None``(manager.py 里 "Returning pre-failure state (None ...)")。于是
    「每一条 INSERT 都失败」在调用方眼里就是「返回 0 条」, 与「种子本来就是
    空的」**完全无法区分**。

    这不是假想: 首次上板卡时 ``prov_schema_version`` 列建成 ``INT`` 而
    ``ProvenanceEntry.version`` 传的是 ``1.0``, 结果 1172 条谱系**一条都没
    进去**, 而本函数返回全 0, 看起来像「种子里没有可灌的东西」。谱系灌不进去
    却报告成功, 比报错糟得多 —— 报错会让人停下, 静默的 0 只会让人以为已经灌过了。
    """
    records = list(seed.get("records") or [])
    failures: list[tuple[str, str]] = []

    def _track(**kw: Any) -> Any:
        return manager.track_entity(
            activity_id=batch_activity,
            agent_id="build_seed_data",
            agent_type="software_agent",
            role="loader",
            **kw,
        )

    n_entity = _count(
        [
            (
                _track(
                    entity_id=str(rec["id"]),
                    entity_type=str(rec.get("entity_type") or "entity"),
                    source=str(rec.get("source") or ""),
                    source_location=_location_of(rec),
                    confidence=rec.get("confidence"),
                    metadata={
                        "authority_kind": _authority_kind_of(rec),
                        "clause": rec.get("clause"),
                        "entity_type": rec.get("entity_type"),
                        # credibility 走 metadata: track_entity 不接受它当
                        # 形参, 由 PG 存储层从此处提到专列(见 pg_storage
                        # 的 _to_param)。这里也留一份, 于是无论从哪边读都一致。
                        "credibility": _credibility_of(rec),
                    },
                ),
                str(rec["id"]),
            )
            for rec in _iter_entity_records(records)
        ],
        failures,
    )

    n_corr = 0
    for corr in _corrections_of(seed):
        # 「这条知识相对方案原文被改过」。它是「同一实体的上一个版本」而不是
        # 「从别处推导」, 所以挂 previous_version_id —— ProvenanceManager 用
        # 这两个字段区分「修正」与「推导」两种关系。
        target = str(corr.get("matched") or corr.get("id") or "")
        if not target:
            continue
        eid = f"correction:{target}"
        entry = _track(
            entity_id=eid,
            entity_type="correction",
            source=str(corr.get("source") or ""),
            metadata={"before": corr.get("before"), "after": corr.get("after")},
            previous_version_id=target,
        )
        n_corr += _tally(entry, eid, failures)

    _raise_if_failed(failures, n_entity + n_corr, "谱系(provenance)")
    logger.info("种子谱系灌入: 实体 %d / 修正 %d", n_entity, n_corr)
    return {"entities": n_entity, "corrections": n_corr}


def build_manager(dsn: str) -> ProvenanceManager:
    """构造一个配了 PG 后端的 ``ProvenanceManager``。"""
    from aterag.provenance.pg_storage import PGProvenanceStorage

    return ProvenanceManager(storage=PGProvenanceStorage(dsn))


# ---------------------------------------------------------------- trace


def load_seed_traces(dsn: str, seed: dict[str, Any]) -> dict[str, int]:
    """把推导边灌进 ``l0_term.trace``。返回写入条数。"""
    import psycopg

    records = list(seed.get("records") or [])
    index = {str(r["id"]): r for r in records if r.get("id")}
    known = set(index)

    rows: list[tuple] = []
    for rec in _iter_derivation_records(records):
        src, dst = str(rec["source_id"]), str(rec["target_id"])
        # 悬空边跳过: 「这条知识由什么推出」的答案里出现一个不存在的对象,
        # 比不给出答案更糟。
        if src not in known or dst not in known:
            continue
        conf = rec.get("confidence")
        clause = rec.get("clause")
        rows.append(
            (
                src,
                str(index[src].get("entity_type") or "entity"),
                dst,
                str(index[dst].get("entity_type") or "entity"),
                str(rec["relationship_type"]),
                clause,
                round(float(conf), 3) if conf is not None else None,
                # verified: 有条款号的推导算已核实(clause 形如 "3.10" /
                # "GB/T 17626.5-2019")。没有 clause 的推导(如
                # applies_to_formula 只说「这条概念用到那条公式」)保持 false,
                # 留给门禁与人审 —— 门禁 G 系列正是看这一列。
                clause is not None,
            )
        )

    if not rows:
        raise RuntimeError("推导边 0 条 —— 种子文件异常(关系记录全被过滤掉了)")

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # 幂等: trace 有 UNIQUE(src_id,src_layer,dst_id,dst_layer,relation),
        # 用 DO UPDATE 而不是 DO NOTHING —— 种子改了 clause 或 confidence 时
        # 必须更新, DO NOTHING 会把旧值永久留下。
        cur.executemany(
            """
            INSERT INTO l0_term.trace
                (src_id, src_layer, dst_id, dst_layer, relation, derivation,
                 confidence, verified)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (src_id, src_layer, dst_id, dst_layer, relation)
            DO UPDATE SET derivation = EXCLUDED.derivation,
                          confidence = EXCLUDED.confidence,
                          verified  = EXCLUDED.verified
            """,
            rows,
        )
        conn.commit()

    logger.info("种子推导边灌入: %d 条 (悬空端点已跳过)", len(rows))
    return {"traces": len(rows)}


# ---------------------------------------------------------------- 辅助


def _tally(entry: Any, eid: str, failures: list[tuple[str, str]]) -> int:
    if entry is None:
        failures.append((eid, "track_entity 返回 None"))
        return 0
    return 1


def _count(calls: list[tuple[Any, str]], failures: list[tuple[str, str]]) -> int:
    """跑 ``(entry, id)`` 对, 计入成功数, 把返回 None 的记进 ``failures``。

    ``track_entity`` 返回 ``None`` 有两种含义: 写失败, **或者**「这是已存在
    实体的纯重标注」(它的 docstring 说成功时返回既有 entry 的 deepcopy)。
    两种都不该静默 —— 前者是故障, 后者说明幂等逻辑没走到预期分支。统一记进
    ``failures`` 让上层抛, 由人判断。
    """
    return sum(_tally(entry, eid, failures) for entry, eid in calls)


def _raise_if_failed(failures: list[tuple[str, str]], total: int, what: str) -> None:
    if failures:
        raise RuntimeError(
            f"{what}灌入失败 {len(failures)}/{total} 条 —— ProvenanceManager 会"
            f"吞掉存储异常并返回 None, 所以「返回 0」与「种子为空」无法区分, "
            f"只能在这里拦。前 5 条失败 id: {[f[0] for f in failures[:5]]}"
        )
    if total == 0:
        raise RuntimeError(f"{what}灌入 0 条 —— 种子文件异常, 请核对。")


__all__ = [
    "CREDIBILITY_BY_AUTHORITY",
    "SEED_ACTIVITY",
    "build_manager",
    "load_seed_provenance",
    "load_seed_traces",
]
