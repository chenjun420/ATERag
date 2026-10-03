"""L0 规则组: rule / rule_parameter / rule_version / borrow_rule / jev_threshold。

Revision ID: 0002_l0_rules
Revises: 0001_l0_base
Create Date: 2026-10-04

来源章节:
    §5.12.1   l0_term.borrow_rule
    §6.5.3    l0_term.disambiguation_log
    §7.4.1    l0_term.rule_parameter        (附录B.1 同表, 一字不改)
    §7.7.1    l0_term.rule_version
    §10.8.1   l0_term.jev_threshold
    §18.6     Step 5/6  规则参数 -> rule (依赖 formula)
    附录B.3   R1-R20 硬规则
    附录B.4   P1-P12 软规则
    §17.6.1   P13-P18 工装软规则
    §18.2.2   Rule dataclass 契约 (formula_ref 必须可解析)

方案未给出 ``CREATE TABLE rule``。本表的列由三处派生并交叉校验:
    1. 附录B.3/B.4 与 §17.6.1 的表格列 (规则 / 名称 / 形式化判据 / 失败动作 /
       公式依据) -> rule_name / formal_criterion / fail_action / formula_ref
    2. §18.6 Step 6 的追溯矩阵 CSV 表头 -> rule_type / used_by_test / standard_ref
    3. §18.2.2 的 ``Rule`` dataclass -> rule_id / body / severity / description
偏离与理由见 docs/adr/ADR-018-rule-table-design.md。

同样重要: 本文件对 spec 的三处 PK 做了**刻意偏离** —— 把 ``valid_from``
并入主键。理由见 ``_PKEY_VERSION_NOTE``, 涉及 borrow_rule / rule_parameter /
jev_threshold 三张表。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_l0_rules"
down_revision: str | None = "0001_l0_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

L0_SCHEMA = "l0_term"

#: §18.2.2 的 severity 枚举。追溯矩阵 CSV 的示例行里 severity 取值与此一致
#: (R13=reject / R16=reject / P13=hold)。
SEVERITIES = ("reject", "hold", "degrade", "hint")

#: 附录B.3/B.4 + §17.6.1 的 fail_action 取值是中文短语 (拒 / 拒或标冲突 /
#: 提示扩容 / 插入前置步 / 阻断工装转qualified / ...), 是自然语言的动作描述
#: 而非闭集枚举。因此 fail_action **不加 CHECK** —— 见 ADR-018 决策 3。

#: §10.8.1 的 jev_threshold 五种 decision_type (从示例 INSERT 反推)。
JEV_DECISION_TYPES = (
    "is_compliant",
    "param_limit",
    "yx_classify",
    "yc_anomaly",
    "setting_score",
)

#: §5.12.1 的 borrow_type 闭集。
BORROW_TYPES = ("term", "rule", "ontology", "template", "formula")

_PKEY_VERSION_NOTE = """\
三张表的 spec 主键不含时间维, 于是 valid_from/valid_until 变成写一次就冻结的
装饰列 —— 与 §5.8.2 的双时态语义直接冲突。

具体地说, 按 spec 的 PK ``(model_schema, rule_id, param_name)``:
    型号 P2 的 bw_limit 从 20MHz 改成 100MHz 时, 新版本与旧版本主键相同,
    只能 UPDATE 覆盖, 于是「某个历史时刻该用哪个 bw_limit」这个问题永远
    无法回答 —— 而 §5.8.3 纪律 3 要求溯源导出走双时态视图, 恰恰是为了
    回答这类问题。

§7.8 也明说「规则版本双时态 | valid_from/valid_until | 支持时间旅行查询」。
所以这里的偏离是把 spec 的 PK 追加 ``valid_from``:
    追加 PK 之后, 「至多一条当前版本」这个不变量改由 partial unique index
    保证 (见 _current_only_index), 与 storage/bitemporal.py 的
    build_partial_unique_index_sql 是同一个机制。
"""


def upgrade() -> None:
    _create_rule()
    _create_rule_parameter()
    _create_rule_version()
    _create_borrow_rule()
    _create_jev_threshold()
    _create_disambiguation_log()


def _current_only_index(schema: str, table: str, columns: str) -> str:
    """「每个 (columns) 至多一条当前版本」的 partial unique index。"""
    return (
        f"CREATE UNIQUE INDEX ux_{table}_current "
        f"ON {schema}.{table} ({columns}) "
        f"WHERE valid_until IS NULL"
    )


def _create_rule() -> None:
    """``l0_term.rule`` —— 方案未给 DDL, 列集由 ADR-018 推导。"""
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.rule (
            rule_id          TEXT PRIMARY KEY
                CHECK (rule_id ~ '^(R[1-9]|R1[0-9]|R20|P[1-9]|P1[0-8])$'),
            rule_type        TEXT NOT NULL
                CHECK (rule_type IN ('hard', 'soft')),
            rule_name        TEXT NOT NULL,
            formal_criterion TEXT NOT NULL,

            -- §18.2.2 的 ``body``: Datalog 规则体 / 表达式原文。
            -- 刻意存原文而不存编译产物: 追溯矩阵 CSV 要导出给人看,
            -- 而 §18.10 注 4 要求通道概念只有一个权威定义。
            body             TEXT NOT NULL,
            expr             TEXT,

            -- 附录B.3 的「公式依据」列。R4/R11/P11/P16 在 spec 里就是空,
            -- 所以可空 —— 但可空必须给理由, 见 ck_formula_ref_reason。
            formula_ref      TEXT
                REFERENCES {L0_SCHEMA}.formula(formula_id)
                ON DELETE RESTRICT,
            formula_section  TEXT,
            no_formula_reason TEXT,

            severity         TEXT NOT NULL
                CHECK (severity IN ({_q_list(SEVERITIES)})),
            fail_action      TEXT NOT NULL,
            domain           TEXT NOT NULL,

            used_by_test     TEXT[],
            standard_ref     TEXT,

            -- §7.8: 每条规则设 owner, 复审周期不超过一个季度。
            owner            TEXT,
            review_cycle     TEXT,

            source_ref       TEXT NOT NULL,
            errata           TEXT,

            -- 刻意**没有** valid_from / valid_until。理由见本函数末尾的注释。

            -- 有公式依据时不得填理由 (理由是给无依据的规则解释「为什么没有」);
            -- 无公式依据时必须填理由。这条约束是 §18.10 注 2「不可追溯的值
            -- 记 UNKNOWN, 绝不猜」的 DDL 落点 —— 不允许悄悄留空。
            CONSTRAINT ck_formula_ref_reason CHECK (
                (formula_ref IS NOT NULL AND no_formula_reason IS NULL)
                OR (formula_ref IS NULL AND no_formula_reason IS NOT NULL)
            )
        )
        """
    )
    # G 门禁的常用查询形态: 「硬规则有哪些」「某公式被哪些规则引用」
    op.execute(f"CREATE INDEX idx_rule_type ON {L0_SCHEMA}.rule (rule_type)")
    op.execute(f"CREATE INDEX idx_rule_formula ON {L0_SCHEMA}.rule (formula_ref)")
    op.execute(f"CREATE INDEX idx_rule_tests ON {L0_SCHEMA}.rule USING GIN (used_by_test)")
    op.execute(f"CREATE INDEX idx_rule_owner ON {L0_SCHEMA}.rule (owner)")

    # 刻意**不**建 (valid_from, valid_until) 索引, 也不建 partial unique index:
    # 本表没有时间列。见 :func:`_create_rule` 里的说明。


def _create_rule_parameter() -> None:
    """§7.4.1 / 附录B.1 的 ``l0_term.rule_parameter``。

    列集与 spec 逐字一致 (含 ``param_type`` 默认 ``'string'``)。
    主键追加 ``valid_from`` —— 见 :data:`_PKEY_VERSION_NOTE`。
    """
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.rule_parameter (
            model_schema TEXT NOT NULL,
            rule_id      TEXT NOT NULL,
            param_name   TEXT NOT NULL,
            param_value  TEXT NOT NULL,
            param_type   TEXT DEFAULT 'string',
            valid_from   TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until  TIMESTAMPTZ,
            PRIMARY KEY (model_schema, rule_id, param_name, valid_from),
            CONSTRAINT ck_rule_param_type CHECK (
                param_type IN ('string', 'int', 'float', 'bool', 'list'))
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_rule_param_model ON {L0_SCHEMA}.rule_parameter (model_schema)"
    )
    op.execute(
        f"CREATE INDEX idx_rule_param_rule ON {L0_SCHEMA}.rule_parameter (rule_id)"
    )
    op.execute(
        _current_only_index(L0_SCHEMA, "rule_parameter", "model_schema, rule_id, param_name")
    )


def _create_rule_version() -> None:
    """§7.7.1 的 ``l0_term.rule_version``。

    主键 (rule_id, version) 已含版本维, 无需偏离 —— 版本历史在这张表里,
    rule 表只存当前有效的那一条。
    """
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.rule_version (
            rule_id       TEXT NOT NULL,
            version       TEXT NOT NULL,
            rule_content  TEXT NOT NULL,
            formula_ref   TEXT,
            owner         TEXT,
            review_cycle  TEXT,
            valid_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until   TIMESTAMPTZ,
            PRIMARY KEY (rule_id, version),
            CONSTRAINT fk_rule_version_rule
                FOREIGN KEY (rule_id)
                REFERENCES {L0_SCHEMA}.rule(rule_id) ON DELETE CASCADE,
            CONSTRAINT fk_rule_version_formula
                FOREIGN KEY (formula_ref)
                REFERENCES {L0_SCHEMA}.formula(formula_id)
                ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_rule_version_valid ON {L0_SCHEMA}.rule_version "
        f"(rule_id, valid_from, valid_until)"
    )


def _create_borrow_rule() -> None:
    """§5.12.1 的 ``l0_term.borrow_rule``。主键追加 valid_from (见 _PKEY_VERSION_NOTE)。"""
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.borrow_rule (
            borrower_schema TEXT NOT NULL,
            lender_schema   TEXT NOT NULL,
            concept_id      TEXT NOT NULL,
            borrow_type     TEXT NOT NULL
                CHECK (borrow_type IN ({_q_list(BORROW_TYPES)})),
            valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until     TIMESTAMPTZ,
            PRIMARY KEY (borrower_schema, lender_schema, concept_id, valid_from),
            -- §5.12.1 的例子里 concept_id 既有 l0_term 的术语 (TRIPPLE_OUTPUT),
            -- 也有规则 ID (P2_RIPPLE_RULE) 与公式 ID (F_J.3_INDUCTOR_RIPPLE)。
            -- 刻意不加到 concept 表的外键: 借用对象跨了三类命名空间,
            -- 加外键等于把这三类强制并成一张表, 而 spec 没有这个意图。
            CONSTRAINT ck_borrow_not_self CHECK (borrower_schema <> lender_schema)
        )
        """
    )
    op.execute(
        _current_only_index(L0_SCHEMA, "borrow_rule", "borrower_schema, lender_schema, concept_id")
    )


def _create_jev_threshold() -> None:
    """§10.8.1 的 ``l0_term.jev_threshold``。主键追加 valid_from (见 _PKEY_VERSION_NOTE)。"""
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.jev_threshold (
            model_schema    TEXT NOT NULL,
            decision_type   TEXT NOT NULL
                CHECK (decision_type IN ({_q_list(JEV_DECISION_TYPES)})),
            threshold       NUMERIC(4,3) NOT NULL
                CHECK (threshold > 0 AND threshold <= 1),
            fallback_action TEXT NOT NULL,
            valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
            valid_until     TIMESTAMPTZ,
            PRIMARY KEY (model_schema, decision_type, valid_from)
        )
        """
    )
    op.execute(_current_only_index(L0_SCHEMA, "jev_threshold", "model_schema, decision_type"))


def _create_disambiguation_log() -> None:
    """§6.5.3 的 ``l0_term.disambiguation_log``。

    这张表是**运行日志**而非知识: 没有 valid_from/valid_until, 也没有
    双时态索引。理由是它的语义是「我们某次把某词消歧成了某概念」——
    这件事只发生一次, 重放它没有意义。给它加双时态列会让审计者误以为
    可以对日志做时间旅行查询。
    """
    op.execute(
        f"""
        CREATE TABLE {L0_SCHEMA}.disambiguation_log (
            log_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            term               TEXT NOT NULL,
            context            TEXT,
            resolved_concept_id TEXT
                REFERENCES {L0_SCHEMA}.concept(concept_id) ON DELETE SET NULL,
            rule_used          TEXT,
            confidence         NUMERIC(3,2)
                CHECK (confidence IS NULL
                       OR (confidence >= 0 AND confidence <= 1)),
            needs_review       BOOLEAN DEFAULT false,
            reviewed_by        TEXT,
            reviewed_at        TIMESTAMPTZ,
            created_at         TIMESTAMPTZ DEFAULT now(),
            -- reviewed_* 必须同时有值或同时为空: 「已复核」与「复核人」不可分割。
            CONSTRAINT ck_reviewed_pair CHECK (
                (reviewed_by IS NULL AND reviewed_at IS NULL)
                OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        f"CREATE INDEX idx_disambig_needs_review "
        f"ON {L0_SCHEMA}.disambiguation_log (created_at) WHERE needs_review"
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.disambiguation_log")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.jev_threshold")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.borrow_rule")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.rule_version")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.rule_parameter")
    op.execute(f"DROP TABLE IF EXISTS {L0_SCHEMA}.rule")


def _q_list(values: tuple[str, ...]) -> str:
    """把字符串元组渲染成 SQL 字符串字面量列表。"""
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


# ``L0_SPEC_ALL`` 只是上面 ``idx_rule_tests`` 用到的 schema 名别名, 保持
# 一处定义, 免得以后改 schema 名时漏改一处。
L0_SPEC_ALL = L0_SCHEMA
