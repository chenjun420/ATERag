"""L0 溯源与推导链: provenance / trace。

Revision ID: 0003_l0_provenance
Revises: 0002_l0_rules
Create Date: 2026-10-05

为什么在 L0 而不是型号 schema
----------------------------
§6.2: ``l0_term`` 放所有型号共用的东西, **绝不复制**。领域知识(概念/公式/
公理/定理/符号/标准)本来就是跨型号共享的, 它的溯源链同理 —— 放进
``pw_<model>.provenance`` 就等于给每个型号存一份, 那正是本项目明确拒绝的
双源。型号自己的溯源(从规格书抽取的需求/保护/信号)留在型号 schema 的
``provenance`` / ``trace``(见 :mod:`aterag.storage.model_schema`)。

为什么 entity_id 是 TEXT 而不是 UUID
-----------------------------------
Semantica 的 ``ProvenanceEntry.entity_id`` 是 ``str``, 而它的 id 空间就是
本项目的 id 空间 —— ``HYSTERESIS`` / ``std::GB 4943.1-2022`` /
``F_J.2.1_BUCK``。型号 schema 里那张 ``provenance`` 用 ``UUID``, 是因为
``pw_*.fact`` 的 ``fact_id`` 确实是 UUID; 领域知识没有这个中间层, 硬造 UUID
只会让「拿 id 查谱系」这件事从一次 SQL 变成一次映射, 且映射一旦不同步就
查不到 —— 而查不到正是最该避免的失败。

哈希链
------
``ProvenanceEntry`` 带 ``sequence_id`` / ``previous_checksum`` /
``checksum``, 由 ``ProvenanceManager._save_entry`` 串成全局插入序的链,
``verify_chain()`` 靠它发现整行删除(删掉中间一行, 后一行的
``previous_checksum`` 就对不上)。SQLite 后端靠 ``BEGIN IMMEDIATE`` 串行化
写入; **PostgreSQL 没有这种隐式串行化**, 两个并发写者会读到同一个链头、
算出同一个 ``sequence_id``。串行化由存储层的 ``pg_advisory_xact_lock`` 负责
(见 :mod:`aterag.provenance.pg_storage` 的 ``transaction()``)。

**``sequence_id`` 刻意不加唯一约束** —— 这一点是踩出来的: 实体已存在时
``track_entity`` 写的归档行**故意复用**同一个 ``sequence_id``, 所以它本来
就不是唯一的。详见下面 ``idx_prov_sequence`` 处的完整记录。

``credibility`` 单独建索引: 冲突消解只按 ``credibility`` 排(不按 recency /
first_seen, 因为种子时间戳大量为空), 那是它唯一的排序键。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_l0_provenance"
down_revision: str | None = "0002_l0_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

L0_SCHEMA = "l0_term"

#: Semantica ``agent_type`` 的取值(``ProvenanceEntry.agent_type``)。
AGENT_TYPES = ("person", "software_agent", "organization")

#: ``trace.relation`` 与 ``provenance.entity_type`` 都**不加 CHECK**。
#: 与 0002 里 ``fail_action`` 的同一处置(见 ADR-018 决策 3): 这两个字段的
#: 取值空间由上游产生且会增长 —— ``relation`` 已经跨了 6 种(种子里的
#: ``applies_to_formula`` / ``has_theorem`` / ``defined_by`` / ``has_unit_kind``
#: 等)加上推理期的 ``inferred``, ``entity_type`` 则是种子的 9 个
#: ``entity_type``。加 CHECK 意味着每加一类就要一次迁移, 而漏掉的那次会让
#: **写入直接失败** —— 谱系表在推理链路上, 写不进去比写得脏更糟。
#: 想要闭集约束的地方是 ``agent_type``, 它的三个取值是上游的硬枚举。


def upgrade() -> None:
    # ---------------- provenance ----------------
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.provenance (
            prov_id           BIGSERIAL PRIMARY KEY,
            -- Semantica 的 entity_id 是 str, 见模块 docstring 的理由。
            --
            -- **UNIQUE 而不是主键**: 上游的存储就是以 entity_id 为主键的
            -- (integrity.compute_checksum 的注释原文: "entity_id is the storage
            -- primary key"), ``store()`` 对同一 entity_id 是**替换**而不是追加。
            -- 本迁移保留 ``prov_id`` 这个代理键, 因为 ``get_chain_head`` 需要
            -- 一个单调的写入序来做平票裁决(同号归档行必须确定性地取到最近
            -- 写入的那一行)—— 那个语义需要「插入序」, 而不是「实体序」。
            entity_id         TEXT NOT NULL UNIQUE,
            entity_type       TEXT NOT NULL,
            activity_id       TEXT NOT NULL,
            agent_id          TEXT NOT NULL DEFAULT 'semantica',
            agent_type        TEXT NOT NULL DEFAULT 'software_agent'
                CHECK (agent_type IN ({_q(AGENT_TYPES)})),
            is_automated      BOOLEAN NOT NULL DEFAULT true,
            role              TEXT,

            source_document   TEXT NOT NULL DEFAULT '',
            source_location   TEXT,
            source_quote      TEXT,

            recorded_at       TEXT NOT NULL,
            first_seen        TEXT,
            last_updated      TEXT,

            confidence        NUMERIC(4,3)
                CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            credibility       NUMERIC(4,3)
                CHECK (credibility IS NULL OR (credibility >= 0 AND credibility <= 1)),
            checksum          TEXT,
            sequence_id       BIGINT,
            previous_checksum TEXT,

            parent_entity_id  TEXT,
            used_entities     TEXT[] NOT NULL DEFAULT '{{}}',
            previous_version_id TEXT,
            derived_from_id   TEXT,

            activity_started_at_time TEXT,
            activity_ended_at_time   TEXT,
            acted_on_behalf_of       TEXT,
            informed_by_activities   TEXT[] NOT NULL DEFAULT '{{}}',

            valid_from        TEXT,
            valid_until       TEXT,
            revision_type     TEXT,
            supersedes        TEXT,
            bundle_id         TEXT,

            invalidated       BOOLEAN NOT NULL DEFAULT false,
            invalidated_at_time TEXT,
            invalidated_by    TEXT,
            invalidation_reason TEXT,

            start_index       INT,
            end_index         INT,

            meta              JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            -- NUMERIC 而不是 INT: ProvenanceEntry.version 传进来是 float
            -- (实测 1.0), 用 INT 列会让**每一次** INSERT 报
            -- 「invalid input syntax for type integer: "1.0"」——
            -- 而 ProvenanceManager 会吞掉存储异常并返回 None, 于是整批
            -- 谱系一条都没进去, 而调用方只看到「返回 0 条」。
            prov_schema_version NUMERIC(4,2)
        )
        """
    )
    # 链的顺序索引用**普通**索引, 不用 UNIQUE。
    #
    # 曾经的错误: 这里建了 UNIQUE, 理由是「并发写者撞链头时让它响」。实测
    # 上板卡第一条就炸了:
    #
    #     ERROR: 重复键违反唯一约束"uq_provenance_sequence"
    #     DETAIL: 键 (sequence_id)=(1042) 已经存在
    #
    # 原因是 ``ProvenanceManager.track_entity`` 在实体已存在时会写一条**归档
    # 历史行**, 并**故意复用同一个 sequence_id** —— 上游注释原文:
    # "Pure relabel: checksum/sequence_id/previous_checksum are left exactly as
    # they were. compute_checksum() excludes entity_id specifically so this is
    # safe"。也就是说 **sequence_id 本来就不是唯一的**: 一个事实的「归档版本」
    # 与它的「现行版本」共享号, 这是设计, 不是脏数据。
    #
    # 并发写者的真实保护是 ``pg_advisory_xact_lock``(见 pg_storage 的
    # transaction()), 那个是在事务内串行化读-改-写序列的; 唯一索引不但不
    # 帮忙, 反而把合法的归档路径堵死了。
    op.execute(
        f"CREATE INDEX idx_prov_sequence ON {L0_SCHEMA}.provenance "
        f"(sequence_id) WHERE sequence_id IS NOT NULL"
    )
    # 谱系查询的主路径: 「这条知识的谱系是什么」
    op.execute(
        f"CREATE INDEX idx_prov_entity ON {L0_SCHEMA}.provenance (entity_id, recorded_at)"
    )
    # 反向谱系(谁推导出我)
    op.execute(
        f"CREATE INDEX idx_prov_parent ON {L0_SCHEMA}.provenance (parent_entity_id)"
    )
    op.execute(
        f"CREATE INDEX idx_prov_derived ON {L0_SCHEMA}.provenance (derived_from_id) "
        f"WHERE derived_from_id IS NOT NULL"
    )
    op.execute(
        f"CREATE INDEX idx_prov_used ON {L0_SCHEMA}.provenance USING gin (used_entities)"
    )
    # 冲突消解只按 credibility 排 —— 它的查询形态就是「取某概念全部来源按
    # credibility 排序」, 不需要额外列, 但需要能快速筛出某概念。
    op.execute(
        f"CREATE INDEX idx_prov_credibility ON {L0_SCHEMA}.provenance "
        f"(entity_id, credibility DESC NULLS LAST)"
    )
    # 墓碑: 「这条被撤回了」必须能查出来, 否则撤回等于消失
    op.execute(
        f"CREATE INDEX idx_prov_invalidated ON {L0_SCHEMA}.provenance (entity_id) "
        f"WHERE invalidated"
    )
    op.execute(
        f"CREATE INDEX idx_prov_activity ON {L0_SCHEMA}.provenance (activity_id)"
    )
    op.execute(
        f"CREATE INDEX idx_prov_bundle ON {L0_SCHEMA}.provenance (bundle_id) "
        f"WHERE bundle_id IS NOT NULL"
    )

    # ---------------- trace ----------------
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.trace (
            trace_id        BIGSERIAL PRIMARY KEY,
            -- 推导链的「当前那一条边」: src 是被解释的对象, dst 是它依赖的东西
            src_id          TEXT NOT NULL,
            src_layer       TEXT NOT NULL,
            dst_id          TEXT NOT NULL,
            dst_layer       TEXT NOT NULL,
            relation        TEXT NOT NULL,
            -- derivation 记「怎么推出来的」(公式串/规则 id/推理链), 是可读的
            -- 解释文本; 与上面的结构化 dst_id 分开, 因为它常是长文本。
            derivation      TEXT,
            confidence      NUMERIC(4,3)
                CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            -- verified=false 表示这条边还没人审 —— 门禁 G 系列要看这个
            verified        BOOLEAN NOT NULL DEFAULT false,
            verified_by     TEXT,
            verified_at     TIMESTAMPTZ,
            prov_id         BIGINT REFERENCES {L0_SCHEMA}.provenance(prov_id)
                ON DELETE SET NULL,
            valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (src_id, src_layer, dst_id, dst_layer, relation)
        )
        """
    )
    op.execute(f"CREATE INDEX idx_l0_trace_src ON {L0_SCHEMA}.trace (src_layer, src_id)")
    op.execute(f"CREATE INDEX idx_l0_trace_dst ON {L0_SCHEMA}.trace (dst_layer, dst_id)")
    op.execute(
        f"CREATE INDEX idx_l0_trace_unverified ON {L0_SCHEMA}.trace (src_layer, src_id) "
        f"WHERE verified = false"
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.trace")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.provenance")


def _q(values: Sequence[str]) -> str:
    """单引号包裹的枚举字面量列表, 供 CHECK 约束内联。

    只拼单引号并把内嵌单引号翻倍 —— 这里的值全是本文件里的 Python 常量,
    不是外部输入。
    """
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)
