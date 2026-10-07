"""给 ``l0_term`` 的按政策不灌数据的表加 ``COMMENT ON TABLE``。

Revision ID: 0004_l0_data_policy
Revises: 0003_l0_provenance
Create Date: 2026-10-07

为什么加注释而不是灌数据
------------------------
``l0_term`` 里 11 张表 (13 张减去有数据的 ``provenance`` / ``trace``) 按
**红线 4 (不接受双源)** 永远不灌种子数据: 领域知识的权威是 git 内版本化的
``data/seed/power_domain_seed.json``, 在 PG 里再放一份就是会漂移的副本。
板卡上曾经存在的 102 条公式副本已确认为死数据并删除
(见 :mod:`aterag.kg.pg_source` 的说明)。

这些表建好后的问题是**审计会把「已决策」读成「已实现」**: 22 个 CHECK、
38 个索引、1 个触发器在 0 行的表上空转, 而 ``docs/adr/README.md`` 里对应的
ADR 全部 ``Accepted``。本迁移把「按政策不灌 + 权威在哪」写在表上 ——
``\\d+`` 与任何审计 SQL 都能直接读到, 不依赖人记得翻 ADR。

**注释是数据政策, 不是免责声明**: 真要往这些表灌数据, 必须先推翻红线 4
(那要一条新 ADR), 而不是绕过这条注释。

``provenance`` / ``trace`` 不在注释之列 —— 它们有数据, 注释只标「不灌」的表,
否则「有注释」会反过来被读成「有问题的表」。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_l0_data_policy"
down_revision: str | None = "0003_l0_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

L0_SCHEMA = "l0_term"

#: 每张按政策不灌数据的表 -> 为什么。
#:
#: 共同尾巴: 数据权威是 data/seed/power_domain_seed.json (红线 4 不接受双源)。
#: 逐条理由只写**这张表为什么是空的**, 不写「将来会灌」—— 那是另一个决策。
POLICY_COMMENTS: dict[str, str] = {
    "formula": (
        "按政策不灌种子数据: 领域公式的权威是 data/seed/power_domain_seed.json, "
        "PG 里再存一份就是红线 4 拒绝的双源 (板卡上曾有的 102 条公式副本已确认为"
        "死数据并删除, 见 src/aterag/kg/pg_source.py)。本表的量纲 CHECK / 索引门禁"
        "(ADR-015) 是为「型号侧自建公式表」准备的, 机制在、数据按政策不进。"
    ),
    "formula_embedding": (
        "按政策不灌: 依赖 formula 有数据, 而 formula 的权威是 "
        "data/seed/power_domain_seed.json, 按红线 4 不灌。触发器 "
        "trg_formula_embedding_dimension_ok (ADR-013 统一 1024 维 halfvec) 机制完备, "
        "但被上游空表挡住 —— 不是坏了。"
    ),
    "standards_registry": (
        "按政策不灌: 标准的权威是种子的 standards 实体 (data/seed/"
        "power_domain_seed.json), 标准正文进 PG 就是红线 4 拒绝的双源。"
        "ADR-002/015 引用的是它的列设计。"
    ),
    "concept": (
        "按政策不灌: 概念/术语层级是领域知识, 权威是 data/seed/"
        "power_domain_seed.json (红线 4 不接受双源)。本表被型号 schema 的 "
        "concept_id 外键引用 (storage/cli.py:212), 外键可以指向 0 行表 —— "
        "术语对齐 (A-22) 若要做, 先决策它属于领域知识还是型号侧, 不能顺手填数据。"
    ),
    "concept_alias": (
        "按政策不灌: 随 concept 同源 (data/seed/power_domain_seed.json 是术语权威), "
        "红线 4 的直接应用。"
    ),
    "disambiguation_log": (
        "按政策不灌: 歧义消解日志的输入是「同一实体的多个候选」, 而候选本体"
        "(concept) 按红线 4 不灌, 无从记录; 数据权威仍是 data/seed/"
        "power_domain_seed.json (ADR-018 设计了列, A-22 记为 policy-blocked)。"
    ),
    "rule": (
        "按政策不灌: 规则的权威是 domain_rules/<域>/rules.yaml, 引擎直读 YAML "
        "(rules_selftest 125/125)。规则结构进 PG 就是第二份可写副本 (红线 4)。"
        "本表列集由三处交叉推导 (ADR-018), 列在而数据不进是有意为之。"
    ),
    "rule_parameter": (
        "按政策不灌: 随 rule 同源 —— domain_rules/<域>/rules.yaml 是规则权威, "
        "规则结构进 PG 就是红线 4 拒绝的第二份可写副本。"
    ),
    "rule_version": (
        "按政策不灌: 规则版本轴 (ADR-018 §7.7.1) 依赖 rule 有数据, 而 rule 按"
        "红线 4 不灌; 规则版本目前由 domain_rules/<域>/rules.yaml 的 version 字段承担。"
    ),
    "borrow_rule": (
        "按政策不灌: 借用规则是规则之间的关系, 规则本身不进 PG (红线 4), 关系"
        "无从落地; 权威是 domain_rules/<域>/rules.yaml (ADR-018)。"
    ),
    "jev_threshold": (
        "按政策不灌: 判据阈值随规则走 (domain_rules/<域>/rules.yaml 是权威), "
        "按红线 4 不进 PG; ADR-018 只设计了列集。"
    ),
}


def upgrade() -> None:
    for table, comment in POLICY_COMMENTS.items():
        op.execute(f"COMMENT ON TABLE {L0_SCHEMA}.{table} IS {_lit(comment)}")


def downgrade() -> None:
    for table in POLICY_COMMENTS:
        op.execute(f"COMMENT ON TABLE {L0_SCHEMA}.{table} IS NULL")


def _lit(value: str) -> str:
    """SQL 字符串字面量: 单引号翻倍。值全是本文件常量, 不是外部输入。"""
    return "'" + value.replace("'", "''") + "'"
