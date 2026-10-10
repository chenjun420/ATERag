"""领域规则 -> 图谱记录(实体 + 关系), 供分析图与 AGE 同步共用。

**为什么把规则放进分析图**
------------------------
板卡实测: ``domain_rules/power/rules.yaml`` 的 130 条规则, 与种子实体的 id
**重叠 0 条**, statement 前 20 字能在种子实体名里找到的也是 0 条。也就是说
分析图里**一条规则都没有** —— 于是

* ``trace_dependency("K-ELEC-001")`` 返回 ``found=false``
* ``analyze_graph`` 的 degree 前几名全是 ``std::`` 标准实体(实测
  ``std::GB/Z 14429-2005`` 排第一), 因为标准是种子边里唯一有连接的一类

规则恰恰是这套知识里**连得最密的骨架**: 130 条 BELONGS_TO / 130 条 IN_SCOPE /
53 条 HAS_FORMULA / 77 条 HAS_CONSTRAINT(AGE 侧实测)。它们缺席, 图必然碎。

**为什么复用而不是各写一份**
--------------------------
``scripts/sync_semantica.py`` 早就有 ``build_knowledge_graph`` 在做同一件事,
但它返回 semantica 的 ``KnowledgeGraph`` 容器, 与 ``collect_records`` 要的
记录格式不同。两边各写一份必然漂 —— 同一批规则在 AGE 与在分析图里长成两个
样子, 那时没人说得清哪个是真的。所以这里出记录格式, ``sync_semantica`` 转成
容器。

**记录格式的来由**
------------------
与 ``load_seed_records`` / ``pg_source.read_model_records`` 一致:
实体带 ``id``/``entity_type``/``name``/``text``/``metadata``, 关系带
``source_id``/``target_id``/``relationship_type``/``confidence``/``clause``。
节点 id 加前缀(``cat:``/``scope:``/``src:``/``formula:``/``shape:``)避免与
种子 id 撞 —— 撞了会静默合并成一个节点, 属性来自其中一方。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: 规则侧节点的 id 前缀。与种子/PG 侧 id 空间隔开, 撞 id 会静默合并节点。
CAT_PREFIX = "cat:"
SCOPE_PREFIX = "scope:"
SRC_PREFIX = "src:"
FORMULA_PREFIX = "formula:"
SHAPE_PREFIX = "shape:"

ENTITY_TYPE_RULE = "domain_rule"
ENTITY_TYPE_CATEGORY = "rule_category"
ENTITY_TYPE_SCOPE = "rule_scope"
ENTITY_TYPE_SOURCE = "rule_source"
ENTITY_TYPE_FORMULA = "rule_formula"
ENTITY_TYPE_SHAPE = "rule_shape"

REL_BELONGS_TO = "BELONGS_TO"
REL_IN_SCOPE = "IN_SCOPE"
REL_CITES = "CITES"
REL_HAS_FORMULA = "HAS_FORMULA"
REL_HAS_CONSTRAINT = "HAS_CONSTRAINT"


def _ent(
    eid: str,
    etype: str,
    name: str,
    text: str,
    confidence: float | None,
    metadata: dict[str, Any],
    source: str = "domain_rules/power/rules.yaml",
) -> dict[str, Any]:
    return {
        "id": eid,
        "entity_type": etype,
        "name": name,
        "text": text,
        "confidence": confidence,
        "source": source,
        "section": None,
        "metadata": metadata,
    }


def _rel(src: str, tgt: str, rtype: str, clause: str = "") -> dict[str, Any]:
    return {
        "source_id": src,
        "target_id": tgt,
        "relationship_type": rtype,
        "confidence": None,
        "clause": clause,
    }


def build_rule_records(
    rules: list[dict[str, Any]],
    *,
    domain: str = "power",
    rules_path: str = "domain_rules/power/rules.yaml",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``rules.yaml`` -> ``(实体, 关系)``。

    另外挂上「规则 -> 领域概念」的**声明式**边: 规则层与种子层是两个独立聚块,
    不跨边就答不出「这条规则算的是哪个量」。实测 ``derive.output`` 的 51 个
    取值里与概念英文名精确相同的只有 1 个, 子串命中的 4 个里 3 个是错的
    (``power`` 会命中 STANDBY_POWER) —— 假边比没边危险, 所以走声明。
    """
    """``rules.yaml`` -> ``(实体, 关系)``。

    缺 ``id`` 的规则**跳过并计数**, 不静默丢弃 —— 那会让「130 条规则进图 129 条」
    这种事实在外面看不见, 而外面看到的仍是「规则都在」。
    """
    ents: list[dict[str, Any]] = []
    rels: list[dict[str, Any]] = []
    skipped: list[str] = []

    for r in rules:
        rid = str(r.get("id") or "").strip()
        if not rid:
            skipped.append(str(r.get("statement") or "<no id>")[:40])
            continue
        derive = r.get("derive") or {}
        constraint = r.get("constraint") or {}
        src = r.get("source") or {}
        statement = str(r.get("statement") or rid)

        ents.append(
            _ent(
                rid,
                ENTITY_TYPE_RULE,
                statement[:120],
                statement,
                r.get("confidence"),
                {
                    "domain": domain,
                    "category": r.get("category", ""),
                    "scope": r.get("scope", ""),
                    "statement": statement,
                    "rule_kind": (
                        "derive" if derive else ("constraint" if constraint else "narrative")
                    ),
                    "derive_expr": derive.get("expr", ""),
                    "derive_output": derive.get("output", ""),
                    "derive_inputs": list(derive.get("inputs") or []),
                    "has_shape": bool(constraint.get("shape")),
                    "test_given": dict((r.get("test") or {}).get("given") or {}),
                    "test_expect": dict((r.get("test") or {}).get("expect") or {}),
                    "source_name": src.get("name", ""),
                    "source_url": src.get("url", ""),
                    "source_retrieved": src.get("retrieved", ""),
                    "skipped_without_id": len(skipped),
                },
                source=rules_path,
            )
        )

        cat = str(r.get("category") or "").strip()
        if cat:
            ents.append(
                _ent(f"{CAT_PREFIX}{cat}", ENTITY_TYPE_CATEGORY, cat, cat, None, {"category": cat})
            )
            rels.append(_rel(rid, f"{CAT_PREFIX}{cat}", REL_BELONGS_TO, cat))

        scope = str(r.get("scope") or "").strip()
        if scope:
            ents.append(
                _ent(
                    f"{SCOPE_PREFIX}{scope}",
                    ENTITY_TYPE_SCOPE,
                    scope,
                    scope,
                    None,
                    {"scope": scope},
                )
            )
            rels.append(_rel(rid, f"{SCOPE_PREFIX}{scope}", REL_IN_SCOPE, scope))

        url = str(src.get("url") or "").strip()
        if url:
            key = f"{SRC_PREFIX}{url}"
            ents.append(
                _ent(
                    key,
                    ENTITY_TYPE_SOURCE,
                    str(src.get("name") or url)[:120],
                    str(src.get("name") or url),
                    None,
                    {"name": src.get("name", ""), "url": url},
                )
            )
            rels.append(_rel(rid, key, REL_CITES, str(src.get("name") or "")[:120]))

        if derive.get("expr"):
            key = f"{FORMULA_PREFIX}{rid}"
            ents.append(
                _ent(
                    key,
                    ENTITY_TYPE_FORMULA,
                    str(derive.get("output") or "result"),
                    str(derive.get("expr") or ""),
                    None,
                    {
                        "expr": derive.get("expr", ""),
                        "output": derive.get("output", ""),
                        "inputs": list(derive.get("inputs") or []),
                    },
                )
            )
            rels.append(_rel(rid, key, REL_HAS_FORMULA, str(derive.get("expr") or "")[:120]))

        if constraint.get("shape"):
            key = f"{SHAPE_PREFIX}{rid}"
            ents.append(
                _ent(
                    key,
                    ENTITY_TYPE_SHAPE,
                    f"{rid} SHACL shape",
                    str(constraint.get("shape"))[:400],
                    None,
                    {"shape": str(constraint.get("shape"))},
                )
            )
            rels.append(_rel(rid, key, REL_HAS_CONSTRAINT, rid))

    return ents, rels


def load_domain_rule_records(
    rules_dir: str | Path,
    domain: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """从 ``<rules_dir>/<domain>/rules.yaml`` 读并构造。

    找不到规则文件时**抛错**而不是返回空: 分析图少了一整层知识, 而症状是
    「追溯不到规则」—— 那会被当成数据缺失去查, 而真实原因是文件不在。
    """
    import yaml

    path = Path(rules_dir) / domain / "rules.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"领域规则文件不存在: {path}。分析图会缺掉整层规则知识 —— "
            f"报错必须指向真实原因, 而不是让『追溯不到 K-ELEC-001』被当成数据缺失。"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules = data.get("rules") if isinstance(data, dict) else data
    ents, rels = build_rule_records(
        list(rules or []),
        domain=domain,
        rules_path=f"{path.as_posix()}",
    )
    rule_nodes = [e for e in ents if e["entity_type"] == ENTITY_TYPE_RULE]
    # 规则 -> 概念 的声明边(见 build_rule_records 的 docstring)
    from aterag.kg.req_concept_edges import build_rule_edges

    x_edges, x_stats = build_rule_edges(list(rules or []))
    rels.extend(x_edges)
    return (
        ents,
        rels,
        {
            "rules": len(rule_nodes),
            "entities": len(ents),
            "relations": len(rels),
            "concept_edges": x_stats["edges"],
            "concept_edges_declared": x_stats["declared"],
        },
    )


__all__ = [
    "CAT_PREFIX",
    "SCOPE_PREFIX",
    "SRC_PREFIX",
    "FORMULA_PREFIX",
    "SHAPE_PREFIX",
    "build_rule_records",
    "load_domain_rule_records",
]
