"""把两类知识合成一张 Semantica ``ContextGraph``, 供 Explorer UI 读。

数据来源与各自权威见 :mod:`aterag.kg.pg_source` 的模块 docstring。合成规则:

* **领域知识** 来自 ``data/seed/power_domain_seed.json``, 节点类型即种子的
  ``entity_type``(``power_concept`` / ``formula`` / ``standard`` / ...),
  关系类型即 ``relationship_type``(``defined_by`` / ``has_unit_kind`` / ...)。
* **型号知识** 来自 PG, 节点类型带 ``model/`` 前缀(``model/Requirement``),
  关系只有 ``has``。

前缀不是装饰: 两类知识的权威不同(一个是 git 内的构建产物, 一个是抽取落库),
追溯审计要求能一眼分清「这条知识的出处是谁」。

**跨来源的边刻意不建**
--------------------
领域概念与型号需求之间确实有关系(某需求约束了某概念), 但抽取层目前不产出
这种边(``aterag_entities`` 只存实体, 关系靠 Product→实体的 ``has`` 结构
隐含)。若在这里按名字猜测着连边, 就等于凭空造出一条没有出处的边 —— 违反
「所有数据标记权威来源」。等抽取层真产出跨来源关系时再加。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aterag.kg import pg_source
from aterag.kg.materialize import build_context_graph, load_seed_records
from aterag.kg.req_concept_edges import build_edges
from aterag.kg.rule_graph import load_domain_rule_records

logger = logging.getLogger(__name__)

#: 种子文件默认位置。可用 ``ATERAG_SEED_PATH`` 覆盖 —— 离线版本包会把种子
#: 放在随包目录下, 板卡上的路径与开发机不同, 不该硬编码。
DEFAULT_SEED_RELPATH = "data/seed/power_domain_seed.json"


def resolve_seed_path(explicit: str | None = None) -> Path:
    """定位种子文件。找不到就抛 —— 不静默返回空图。

    静默退化成「空图 + 一条日志」是最坏的一种失败: Explorer 正常起来、页面
    正常渲染、只是没有数据, 看起来像「这个型号本来就没抽取出东西」。
    """
    import os

    cand = explicit or os.getenv("ATERAG_SEED_PATH") or DEFAULT_SEED_RELPATH
    p = Path(cand)
    if not p.is_absolute() and not p.exists():
        # 从包根往上找: 板卡上 /opt/aterag 是部署根, cwd 可能不是它
        for parent in [Path.cwd(), *Path(__file__).resolve().parents]:
            alt = parent / cand
            if alt.exists():
                p = alt
                break
    if not p.exists():
        raise FileNotFoundError(
            f"找不到领域知识种子 {cand!r}。领域知识的权威是种子文件(不灌 PG, 见 "
            f"kg/pg_source 模块 docstring), 缺它就没有可物化的知识。指向办法: "
            f"设置环境变量 ATERAG_SEED_PATH, 或从仓库根目录启动。"
        )
    return p


def collect_records(
    dsn: str | None,
    *,
    seed_path: str | None = None,
    model_ids: list[str] | None = None,
    domain: str = "power",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读三类知识, 返回 ``(实体, 关系)``。

    ==================  ==========================  =========================
    层                  来源                        为什么单列
    ==================  ==========================  =========================
    领域概念/公式/标准  种子(``power_domain_seed``)  版本化构建产物
    规则               ``domain_rules/<d>/``        130 条, 与种子 id 零重叠
    型号需求/保护/信号  PostgreSQL                  抽取产物, 随规格书变
    ==================  ==========================  =========================

    三层之间**默认没有任何边** —— 各自是独立权威。不补边的话图必然碎: 实测
    只用种子时可达比例 8.1%(最大连通分量 48/594), 而 204 条需求与 130 条规则
    都查不到依据。跨层边由 :mod:`req_concept_edges`(声明式)与本模块的规则层
    补, 实测合并后可达 40.0%。

    ``dsn`` 为 None 时**不含型号层** —— 供不接库的静态检查/单测使用。
    真实服务必须给 dsn, 否则型号知识整块缺失, 而那正是 Explorer 最该看的
    一部分。
    """
    ents: list[dict[str, Any]] = []
    rels: list[dict[str, Any]] = []

    s_ents, s_rels = load_seed_records(str(resolve_seed_path(seed_path)))
    ents.extend(s_ents)
    rels.extend(s_rels)
    logger.info("领域知识: %d 实体 / %d 关系 (来自种子)", len(s_ents), len(s_rels))

    # 规则层: 实测 130 条规则与种子 id **零重叠**, 不加这一层, 分析图里
    # 一条规则都没有 —— `trace_dependency("K-ELEC-001")` 直接 found=false,
    # 而 degree 排名前列全被 `std::` 标准实体占满(标准是种子里唯一有连接的类)。
    try:
        from aterag.config import get_settings

        rules_dir = get_settings().domain_rules_dir
        r_ents, r_rels, r_stats = load_domain_rule_records(rules_dir, domain)
        ents.extend(r_ents)
        rels.extend(r_rels)
        logger.info(
            "规则知识: %d 条规则 / %d 实体 / %d 关系",
            r_stats["rules"],
            r_stats["entities"],
            r_stats["relations"],
        )
    except FileNotFoundError as e:
        logger.warning("规则层缺席(可追溯性下降, 不静默): %s", e)

    if dsn:
        m_ents, m_rels = pg_source.read_model_records(dsn, model_ids=model_ids)
        ents.extend(m_ents)
        rels.extend(m_rels)
        logger.info("型号知识: %d 实体 / %d 关系 (来自 PG)", len(m_ents), len(m_rels))
        # 跨权威的边: 型号侧(需求) <-> 领域侧(概念)。没有它, 合并图里
        # 「这条需求依据什么」答不出来 —— 实测 SR-1204 找到节点但 reachable=0。
        x_rels, x_stats = build_edges(m_ents)
        rels.extend(x_rels)
        logger.info(
            "需求->概念映射: %d 条声明 / %d 条需求命中 / %d 条边 (未映射需求 %d)",
            x_stats["declared"],
            x_stats["matched_requirements"],
            x_stats["edges"],
            x_stats["unmapped_requirements"],
        )

    return ents, rels


def build_graph(
    dsn: str | None,
    *,
    seed_path: str | None = None,
    model_ids: list[str] | None = None,
    advanced_analytics: bool = False,
    domain: str = "power",
):
    """合成 ``ContextGraph``(种子 + 规则 + 型号三层, 见 :func:`collect_records`)。

    ``advanced_analytics`` 默认关: 它会拉 gensim(实测未装, 只打一条告警后
    降级), 而 Explorer 的节点/边浏览、搜索、邻接查询都不需要它 —— 打开只
    多一个依赖与一份启动耗时。要用中心性/社区发现时再显式开。
    """
    ents, rels = collect_records(dsn, seed_path=seed_path, model_ids=model_ids, domain=domain)
    graph, n_nodes, n_edges = build_context_graph(
        [*ents, *rels], [], advanced_analytics=advanced_analytics
    )
    logger.info("ContextGraph: %d 节点 / %d 边", n_nodes, n_edges)
    return graph, {
        "nodes": n_nodes,
        "edges": n_edges,
        "entities": len(ents),
        "relations": len(rels),
    }


def build_session(
    dsn: str | None,
    *,
    seed_path: str | None = None,
    model_ids: list[str] | None = None,
    advanced_analytics: bool = False,
):
    """合成图并包成 ``GraphSession`` —— Explorer ``create_app(session=...)`` 要的形态。"""
    from semantica.explorer.session import GraphSession

    graph, stats = build_graph(
        dsn,
        seed_path=seed_path,
        model_ids=model_ids,
        advanced_analytics=advanced_analytics,
    )
    return GraphSession(graph), stats


def loaders_status(dsn: str | None) -> dict[str, Any]:
    """两个数据源各自的状态, 供 ``/health`` 暴露。

    分开报而不是合成一个 ok/false: 「种子没找到」与「PG 连不上」是完全不同
    的故障, 合成一个布尔就丢了定位信息。
    """
    out: dict[str, Any] = {"seed": None, "postgres": None}
    try:
        p = resolve_seed_path()
        data = json.loads(p.read_text(encoding="utf-8"))
        out["seed"] = {"ok": True, "path": str(p), "records": len(data.get("records") or [])}
    except Exception as exc:  # noqa: BLE001
        out["seed"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if dsn:
        try:
            out["postgres"] = {"ok": True, **pg_source.graph_stats(dsn)}
        except Exception as exc:  # noqa: BLE001
            out["postgres"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        out["postgres"] = {"ok": False, "error": "未配置 dsn, 只加载了领域知识"}
    return out


__all__ = [
    "DEFAULT_SEED_RELPATH",
    "build_graph",
    "build_session",
    "collect_records",
    "loaders_status",
    "resolve_seed_path",
]
