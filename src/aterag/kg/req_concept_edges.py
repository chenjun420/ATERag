"""需求 -> 领域概念 的声明式映射, 读出来变成图谱里的边。

**为什么必须有这些边**
--------------------
型号知识(204 个 Requirement)在 PG, 领域知识(概念/公式/标准)在种子, 两边
本来就各管一摊 —— ``pg_source.py`` 的模块 docstring 写明「两类知识, 两个权威,
互不重叠」。但**互不重叠的代价**是图谱里没有一条边跨过去: 实测合并图
``trace_dependency("SR-PA601-D54A-1204@unit=W")`` 找到节点但 ``reachable=0``,
「这条需求依据什么」这个问题答不出来。

自动匹配解决不了: 实测 204 条需求标题与 194 个 power_concept 的 text
**完全相同的 0 条**。模糊匹配能凑出几百条边, 但假边比没边更危险 —— 追溯会
给出「看似合理的错误依据」, 而产线会照着它设计测试。所以这里逐条声明,
每条带依据, 覆盖度如实报出(当前 8 条需求 / 约 4%)。

**不静默的部分**
----------------
* 声明的概念不在种子里 -> 抛错(否则那条边指向一个不存在的节点, 图会显示为
  「追溯断了」而不是「映射写错了」)
* 声明的需求不在 PG 里 -> 跳过并计数, 不抛错(某个型号可能还没抽取完)
* 覆盖度随每次建图一起返回, 让「图连通性差」与「映射没写」可区分
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 关系类型名。与种子里既有的 ``has_theorem`` / ``defined_by`` 同一命名风格。
EDGE_TYPE = "measured_by"


@dataclass(frozen=True)
class ConceptEdge:
    """一条「需求测的是哪个领域概念」的声明。"""

    req_id: str
    concept: str
    basis: str
    authority_kind: str = "spec"
    rail: str | None = None

    @property
    def relation_type(self) -> str:
        return EDGE_TYPE


def _seed_path() -> Path:
    # parents[3] 才是仓库根(本文件在 <repo>/src/aterag/kg/)。少算一层会指向
    # <repo>/src/data/... —— 声明文件读不到, 于是 build_edges 安静地返回 0 条边,
    # 症状是「代码接好了但图没变化」, 极难定位。
    return Path(__file__).resolve().parents[3] / "data" / "seed" / "power_domain_seed.json"


def _edges_path() -> Path:
    return _seed_path().parent / "requirement_concept_edges.yaml"


def _seed_concept_ids() -> set[str]:
    data = json.loads(_seed_path().read_text(encoding="utf-8"))
    return {str(r.get("id")) for r in (data.get("records") or []) if r.get("id")}


def load_declarations() -> list[ConceptEdge]:
    """读声明文件并**校验概念存在**。

    校验失败抛错而不是跳过: 写错一个概念 id 时, 跳过会让那条边消失, 而图上
    表现为「追溯到一半断了」—— 那是个会被误读成数据缺失的现象。映射写错应该
    在加载时就炸。
    """
    import yaml

    path = _edges_path()
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = _seed_concept_ids()
    out: list[ConceptEdge] = []
    for i, raw in enumerate(data.get("edges") or []):
        basis = str(raw.get("basis") or "").strip()
        if not basis:
            raise ValueError(f"{path.name} 第 {i + 1} 条缺 basis —— 每条映射都要写清依据")
        concept = str(raw["concept"]).strip()
        if concept not in known:
            raise ValueError(
                f"{path.name} 第 {i + 1} 条的概念 {concept!r} 不在种子里 "
                f"(种子共 {len(known)} 个 id)。写成不存在的概念会让那条边指向空节点, "
                f"图上表现为「追溯断了」而不是「映射写错了」。"
            )
        out.append(
            ConceptEdge(
                req_id=str(raw["req_id"]).strip(),
                concept=concept,
                basis=basis,
                authority_kind=str(raw.get("authority_kind") or "spec"),
                rail=raw.get("rail"),
            )
        )

    # 规则 -> 概念。规则 id 直接就是图里的节点 id(``rule_graph`` 产出的
    # ``K-XXX-NNN``), 所以不需要像需求那样经 req_id 解析。
    for i, raw in enumerate(data.get("rule_edges") or []):
        basis = str(raw.get("basis") or "").strip()
        if not basis:
            raise ValueError(f"{path.name} rule_edges 第 {i + 1} 条缺 basis")
        concept = str(raw["concept"]).strip()
        if concept not in known:
            raise ValueError(f"{path.name} rule_edges 第 {i + 1} 条的概念 {concept!r} 不在种子里")
        out.append(
            ConceptEdge(
                req_id=str(raw["rule"]).strip(),
                concept=concept,
                basis=basis,
                authority_kind=str(raw.get("authority_kind") or "spec"),
                rail=None,
            )
        )
    return out


def build_rule_edges(rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """规则侧的声明边。规则 id 就是节点 id, 直接连。"""
    decls = [d for d in load_declarations() if d.req_id.startswith("K-")]
    rule_ids = {str(r.get("id") or "") for r in rules}
    edges: list[dict[str, Any]] = []
    matched: set[str] = set()
    skipped: list[str] = []
    for d in decls:
        if d.req_id not in rule_ids:
            skipped.append(d.req_id)
            continue
        matched.add(d.req_id)
        edges.append(
            {
                "id": f"{EDGE_TYPE}::{d.req_id}->{d.concept}",
                "source_id": d.req_id,
                "target_id": d.concept,
                "relationship_type": EDGE_TYPE,
                "confidence": 1.0,
                "clause": d.basis.strip().splitlines()[0][:160],
                "properties": {
                    "basis": d.basis.strip(),
                    "authority_kind": d.authority_kind,
                    "rule_id": d.req_id,
                    "declared": True,
                },
            }
        )
    return edges, {
        "declared": len(decls),
        "matched_rules": len(matched),
        "edges": len(edges),
        "skipped_not_in_rules": skipped,
    }


def build_edges(
    entities: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按声明生成边。

    ``entities`` 是 PG 侧读出的型号实体(取自 :func:`collect_records` 的同一批),
    以它们的 ``eid`` 与 ``props.req_id`` 为匹配依据。

    匹配用 ``props["req_id"]`` 而非 eid 前缀 —— eid 里编码了 rail/unit/工况
    变体(``SR-PA601-D54A-1203@rail=-54V+duty_long_term=长期+unit=A``),
    抽取规则一改就变; ``req_id`` 才是稳定身份。
    """
    decls = load_declarations()

    # ``pg_source.read_model_records`` 产出的实体把抽取原始字段放在
    # ``metadata`` 下(实测键: entity_type/id/metadata/name/section/source/text),
    # **不是** ``properties`` —— 写错键名会让所有声明静默匹配 0 条, 而代码
    # 看起来完全正常。这里两种键都认, 但以 metadata 为准。
    def _props(e: dict[str, Any]) -> dict[str, Any]:
        return e.get("metadata") or e.get("properties") or {}

    by_req: dict[str, list[dict[str, Any]]] = {}
    for e in entities:
        rid = str(_props(e).get("req_id") or "").strip()
        if rid:
            by_req.setdefault(rid, []).append(e)

    edges: list[dict[str, Any]] = []
    matched_reqs: set[str] = set()
    skipped: list[str] = []
    for d in decls:
        cands = by_req.get(d.req_id) or []
        if d.rail is not None:
            cands = [c for c in cands if str(_props(c).get("rail") or "") == d.rail]
        if not cands:
            skipped.append(d.req_id)
            continue
        matched_reqs.add(d.req_id)
        for c in cands:
            src = str(c.get("id"))
            edges.append(
                {
                    "id": f"{EDGE_TYPE}::{src}->{d.concept}",
                    "source_id": src,
                    "target_id": d.concept,
                    "relationship_type": d.relation_type,
                    "confidence": 1.0,
                    "clause": d.basis.strip().splitlines()[0][:160],
                    "properties": {
                        "basis": d.basis.strip(),
                        "authority_kind": d.authority_kind,
                        "req_id": d.req_id,
                        "declared": True,
                    },
                }
            )

    all_reqs = {str(_props(e).get("req_id") or "").strip() for e in entities}
    all_reqs.discard("")
    total_reqs = len(all_reqs)
    stats = {
        "declared": len(decls),
        "matched_requirements": len(matched_reqs),
        "edges": len(edges),
        "unmapped_requirements": max(total_reqs - len(matched_reqs), 0),
        "skipped_not_in_pg": skipped,
    }
    return edges, stats


__all__ = [
    "EDGE_TYPE",
    "ConceptEdge",
    "build_edges",
    "build_rule_edges",
    "load_declarations",
]
