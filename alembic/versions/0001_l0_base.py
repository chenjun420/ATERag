"""L0 基线: 扩展 + l0_term + formula + standards_registry

Revision ID: 0001_l0_base
Revises:
Create Date: 2026-10-04

来源章节:
    §6.2.1   l0_term.concept / l0_term.concept_alias
    §18.3.1  formula / formula_embedding
    §18.5    扩展清单与执行顺序 (第 0 节与第 1 节)
    §18.6    装载顺序步骤 1~4 的表

这一版只建 L0 共享层。``pw_<model_key>`` 的型号 schema 不进版本库 ——
理由见 alembic/env.py 的「型号 schema 的处理」。

全部用 ``op.execute`` 发原始 DDL 而非 SQLAlchemy 的 Table/Catalog 对象。
原因是 §18.5 要求 ``seed/schema_full.sql`` 可直接 psql 执行, 而用 sa
对象拼出来的 DDL 顺序与格式不可控 (外键顺序、CHECK 的位置由 SQLAlchemy
内部决定), 审计者读到的东西与实际执行的结构会不一致。原始 DDL 让
「文件里写的就是执行的」。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_l0_base"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: §6.2: L0 共享 schema。所有型号共用, 绝不复制。
L0_SCHEMA = "l0_term"

#: §18.3.1 用 halfvec(1024) 与本项目的 ADR-013 一致 (方案该处写
#: vector(4096), 是把模型原生维度误当部署维度; 详见 ADR-013)。
EMBED_DIM = 1024

#: §6.2.1 的十类 kind。
CONCEPT_KINDS = (
    "measurand",
    "instrument",
    "condition",
    "action",
    "policy",
    "status_signal",
    "telemetry",
    "protection",
    "setting",
    "command",
)

#: §6.2.1 的四类 alias_type。
ALIAS_TYPES = ("synonym", "abbreviation", "misspelling", "legacy")

#: §18.3.1 formula.scope 的三个取值。
FORMULA_SCOPES = ("global", "shared_l0", "model")


def upgrade() -> None:
    # ---- 0. 扩展 (§18.5 第 0 节) ----
    # 刻意不装 timescaledb: 它是可选扩展 (storage/schema.py 的
    # OPTIONAL_EXTENSIONS), 板卡部署与容器部署的可用扩展集不同,
    # 在迁移里硬要求会把容器路径拖死。hypertable 的创建由
    # docgen.ddl 在确认扩展存在后进行。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS age")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_textsearch")
    op.execute("CREATE EXTENSION IF NOT EXISTS zhparser")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ---- L0 schema ----
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{L0_SCHEMA}"')

    _create_concept_tables()
    _create_formula_tables()
    _create_standards_registry()


def _create_concept_tables() -> None:
    """§6.2.1 受控术语表。"""
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.concept (
            concept_id       TEXT PRIMARY KEY,
            pref_label_zh    TEXT NOT NULL,
            pref_label_en    TEXT,
            aliases          TEXT[],
            forbidden_terms  TEXT[],
            kind             TEXT NOT NULL
                CHECK (kind IN ({_q_list(CONCEPT_KINDS)})),
            qudt_ref         TEXT,
            maps_to          TEXT,
            owner            TEXT,
            review_cycle     TEXT,
            borrow_scope     TEXT,
            valid_from       TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until      TIMESTAMPTZ,
            created_at       TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.concept_alias (
            alias       TEXT PRIMARY KEY,
            concept_id  TEXT NOT NULL
                REFERENCES {L0_SCHEMA}.concept(concept_id) ON DELETE CASCADE,
            alias_type  TEXT NOT NULL
                CHECK (alias_type IN ({_q_list(ALIAS_TYPES)})),
            confidence  NUMERIC(3,2) DEFAULT 1.0
                CHECK (confidence >= 0 AND confidence <= 1),
            valid_from  TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until TIMESTAMPTZ
        )
        """
    )
    # §6.2.1 的三个索引
    op.execute(
        f"CREATE INDEX idx_alias_concept ON {L0_SCHEMA}.concept_alias (concept_id)"
    )
    op.execute(f"CREATE INDEX idx_concept_kind ON {L0_SCHEMA}.concept (kind)")
    op.execute(
        f"CREATE INDEX idx_concept_aliases ON {L0_SCHEMA}.concept USING gin (aliases)"
    )
    op.execute(
        f"CREATE INDEX idx_concept_forbidden "
        f"ON {L0_SCHEMA}.concept USING gin (forbidden_terms)"
    )


def _create_formula_tables() -> None:
    """§18.3.1 公式表。

    dimension_vec 用 NUMERIC(8,4)[] 而非 INT[]: sqrt 会把量纲分量折半,
    整数类型存不下。见 solver/symbolic.py 的 Dimension 注释。
    """
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.formula (
            formula_id      TEXT PRIMARY KEY,
            alias           TEXT[],
            name_zh         TEXT NOT NULL,
            name_en         TEXT,
            domain          TEXT NOT NULL,
            section         TEXT NOT NULL,
            partition       TEXT,
            seq             INT,

            expr_latex      TEXT NOT NULL,
            expr_plaintext  TEXT NOT NULL,
            expr_ascii      TEXT NOT NULL,
            expr_ast        JSONB NOT NULL,

            var_refs        TEXT[] NOT NULL,
            dimension_vec   NUMERIC(8,4)[] NOT NULL,
            dimension_ok    BOOLEAN NOT NULL DEFAULT true,

            derive_from     TEXT[] NOT NULL,
            boundary        TEXT,
            confidence      NUMERIC(3,2) NOT NULL DEFAULT 0.95
                CHECK (confidence >= 0 AND confidence <= 1),

            scope           TEXT NOT NULL DEFAULT 'global'
                CHECK (scope IN ({_q_list(FORMULA_SCOPES)})),
            model_key       TEXT,

            equivalent_ids  TEXT[],
            used_by_rule    TEXT[],
            used_by_test    TEXT[],
            used_by_axon    TEXT[],

            domain_tags     TEXT[] NOT NULL,
            errata          TEXT,
            source_ref      TEXT NOT NULL,
            valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until     TIMESTAMPTZ,

            CONSTRAINT ck_scope_model CHECK (
                (scope <> 'model') OR (model_key IS NOT NULL)),
            CONSTRAINT ck_model_scope CHECK (
                (scope = 'model') OR (model_key IS NULL)),
            -- §18.9 G1: 量纲向量的分量数必须等于 7 (A-11)。
            -- 用 cardinality 而不是逐值比对: 值的正确性由
            -- solver/symbolic.py 在入库前保证, DDL 只需保证个数对。
            CONSTRAINT ck_dimension_arity CHECK (
                cardinality(dimension_vec) = 7)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.formula_embedding (
            formula_id TEXT PRIMARY KEY
                REFERENCES {L0_SCHEMA}.formula(formula_id) ON DELETE CASCADE,
            content    TEXT NOT NULL,
            embedding  vector({EMBED_DIM})
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_formula_emb ON {L0_SCHEMA}.formula_embedding "
        f"USING hnsw (embedding halfvec_cosine_ops)"
    )

    # 反向索引: 追溯链的数据基础 (§1.6 原则六)
    op.execute(f"CREATE INDEX idx_formula_rule ON {L0_SCHEMA}.formula USING GIN (used_by_rule)")
    op.execute(f"CREATE INDEX idx_formula_test ON {L0_SCHEMA}.formula USING GIN (used_by_test)")
    op.execute(
        f"CREATE INDEX idx_formula_derive ON {L0_SCHEMA}.formula USING GIN (derive_from)"
    )
    op.execute(
        f"CREATE INDEX idx_formula_equiv ON {L0_SCHEMA}.formula USING GIN (equivalent_ids)"
    )
    op.execute(f"CREATE INDEX idx_formula_tags ON {L0_SCHEMA}.formula USING GIN (domain_tags)")
    op.execute(f"CREATE INDEX idx_formula_alias ON {L0_SCHEMA}.formula USING GIN (alias)")
    op.execute(f"CREATE INDEX idx_formula_domain ON {L0_SCHEMA}.formula (domain, section)")
    # G1 门禁的常用查询: 只看量纲齐全的
    op.execute(
        f"CREATE INDEX idx_formula_dim_ok ON {L0_SCHEMA}.formula (dimension_ok)"
    )

    # ---- G1 门禁的数据库级兜底 ----
    # §18.9 G1 + ADR-015 决策 2: dimension_ok=false 的行不得进入向量索引。
    #
    # 用触发器而不是 view/查询约定: 触发器是**唯一**能拦住「先 INSERT 进
    # formula, 再 INSERT 进 formula_embedding」这一顺序的机制 —— 而这正是
    # 装载管线实际会做的两步。只在 formula_embedding 上加 CHECK 是做不到的,
    # 因为 CHECK 看不到同一事务里刚写的另一张表。
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {L0_SCHEMA}.assert_formula_dimension_ok()
        RETURNS TRIGGER AS $$
        DECLARE
            ok BOOLEAN;
        BEGIN
            SELECT dimension_ok INTO ok
              FROM {L0_SCHEMA}.formula
             WHERE formula_id = NEW.formula_id;
            IF ok IS FALSE THEN
                RAISE EXCEPTION
                    '公式 % 的 dimension_ok=false, 不得进入向量索引 (§18.9 G1)',
                    NEW.formula_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_formula_embedding_dimension_ok
        BEFORE INSERT OR UPDATE ON {L0_SCHEMA}.formula_embedding
        FOR EACH ROW EXECUTE FUNCTION {L0_SCHEMA}.assert_formula_dimension_ok()
        """
    )


def _create_standards_registry() -> None:
    """标准登记表。

    §18.6 步骤 3: 装附录V 的 ≥60 条。G6 门禁校验
    ``formula.source_ref`` 落在本表的 ``std_code`` 内。
    """
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.standards_registry (
            std_code      TEXT PRIMARY KEY,
            title_zh      TEXT NOT NULL,
            title_en      TEXT,
            issuing_body  TEXT,
            std_kind      TEXT NOT NULL
                CHECK (std_kind IN (
                    'national', 'industry', 'military', 'international',
                    'company', 'internal')),
            version       TEXT,
            published_on  DATE,
            superseded_by TEXT,
            domain_tags   TEXT[] NOT NULL,
            source_ref    TEXT NOT NULL,
            url           TEXT,
            retrieved_at  TIMESTAMPTZ,
            notes         TEXT,
            valid_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until   TIMESTAMPTZ
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_standards_kind ON {L0_SCHEMA}.standards_registry (std_kind)"
    )
    op.execute(
        f"CREATE INDEX idx_standards_tags ON {L0_SCHEMA}.standards_registry "
        f"USING GIN (domain_tags)"
    )
    # 「现行标准」是 G6 的默认视角。partial index 让该查询走索引。
    op.execute(
        f"CREATE INDEX idx_standards_current ON {L0_SCHEMA}.standards_registry "
        f"(std_code) WHERE superseded_by IS NULL"
    )


def downgrade() -> None:
    """可执行的回退。

    顺序与 upgrade 相反: 触发器/函数 -> 索引随表 DROP -> 表 -> schema。
    不删 L0_SCHEMA 本身: 它可能已被其它对象引用 (如 AGE 图),
    删 schema 是比删表更大的动作, 不该由这一版迁移隐式完成。
    """
    op.execute(f"DROP TRIGGER IF EXISTS trg_formula_embedding_dimension_ok "
               f"ON {L0_SCHEMA}.formula_embedding")
    op.execute(f"DROP FUNCTION IF EXISTS {L0_SCHEMA}.assert_formula_dimension_ok()")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.formula_embedding")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.formula")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.standards_registry")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.concept_alias")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.concept")


def _q_list(values: tuple[str, ...]) -> str:
    """把字符串元组渲染成 SQL 字符串字面量列表。"""
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)
