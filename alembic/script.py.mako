"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """必须给出可执行的回退。

    空 downgrade 不是「不需要回退」, 而是「还没想好怎么回退」——
    两者在代码上无法区分, 所以一律要求写出来。若某个对象确实无法回退
    (如已进生产的数据), 在函数体里显式 raise 而不是留空, 并在注释里
    说明原因。
    """
    ${downgrades if downgrades else 'raise NotImplementedError("待实现回退")'}