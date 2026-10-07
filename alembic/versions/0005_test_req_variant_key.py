"""给 ``pw_*.test_requirement`` 加 ``variant_key``, 并建 (sr_id, variant_key) 唯一索引。

Revision ID: 0005_test_req_variant_key
Revises: 0004_l0_data_policy
Create Date: 2026-10-07

为什么必须加这一列
------------------
实测 PA601: 抽取产出 **95 个条件只对应 74 个 ``sr_id``** —— 18 个编号各有 2~3 档
(多电压轨 / 多负载点 / 长期 vs 短期)。例:

- ``SR-PA601-D54A-1104`` 功率因数 3 档(20%/50%/满载)
- ``SR-PA601-D54A-1210`` 整机效率 3 档
- ``SR-PA601-D54A-1203`` 输出电流 3 行(-54V 长期/-54V 短期/3.45V 长期)

而 ``test_requirement`` 原本只有 ``sr_id`` 且**无唯一约束**。后果有两个, 都是静默的:

1. 按 ``sr_id`` upsert -> 同一编号的 N 档互相覆盖, 落库后只剩 74 行, 而抽取
   产出 95 行。差异无任何报错。
2. 想「一行一档」就得让 ``sr_id`` 唯一 -> 那会把多档位判据合并成一档, 正是
   :mod:`aterag.ingest.entity_extract` 的 eid 档位后缀当初要解决的问题(改判据就
   换 id / 多档位被合并)。

``variant_key`` 的取值与 eid 档位后缀同构: **电压轨 + 工况标签, 不含判据数值**。
判据数值进主键的话, 改一次判据就等于换一行主键, 判据无法版本化。

历史行怎么办
------------
迁移前若已有行(实测 0 行, 但迁移脚本必须对「有数据」成立), 它们没有档位概念 ——
统一填 ``''``, 与 ``entity_extract`` 里 ``rail=''``(未标注轨)的既有语义一致。
因此唯一索引建得起来, 且不会因为多档位而失败。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_test_req_variant_key"
down_revision: str | None = "0004_l0_data_policy"
branch_labels: str | None = None
depends_on: str | None = None


def _schemas() -> Sequence[str]:
    """所有 ``pw_*`` 型号 schema(``test_requirement`` 是按型号建的)。

    ``LIKE 'pw\\_%'``: 反斜杠是 LIKE 的默认转义符, 不转义的话 ``_`` 会匹配任意
    单字符, 把 ``public`` 之类的无关 schema 也捞进来。
    """
    rows = op.get_bind().execute(
        sa.text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'pw\\_%' ORDER BY schema_name"
        )
    ).fetchall()
    return [r[0] for r in rows]


def _has_table(schema: str, table: str) -> bool:
    """表在不在。

    显式查而不是靠 try/except: Alembic 迁移里吞异常会让「这版迁移没生效」变成
    无声无息的事, 而 DDL 失败本来就应该让人看见。
    """
    return bool(
        op.get_bind()
        .execute(
            sa.text("SELECT to_regclass(:qualified) IS NOT NULL"),
            {"qualified": f"{schema}.{table}"},
        )
        .scalar()
    )


def upgrade() -> None:
    for s in _schemas():
        if not _has_table(s, "test_requirement"):
            continue
        op.execute(
            f"ALTER TABLE {s}.test_requirement "
            f"ADD COLUMN IF NOT EXISTS variant_key TEXT NOT NULL DEFAULT ''"
        )
        # 幂等: IF NOT EXISTS 让「已建过」的库重跑不报错。
        op.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS uq_req_sr_variant "
            f"ON {s}.test_requirement (sr_id, variant_key)"
        )


def downgrade() -> None:
    for s in _schemas():
        if not _has_table(s, "test_requirement"):
            continue
        op.execute(f"DROP INDEX IF EXISTS {s}.uq_req_sr_variant")
        op.execute(
            f"ALTER TABLE {s}.test_requirement DROP COLUMN IF EXISTS variant_key"
        )
