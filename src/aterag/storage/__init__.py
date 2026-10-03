"""L0 存储底座。

层职责 (V6.0 §18.1.3):
    schema.py       建库、扩展安装、schema-per-型号 pw_<model_key>
    bitemporal.py   双时态 DAO (valid_from / valid_until)
    rls.py          行级安全策略
    registry.py     型号登记与解析
    healthcheck.py  启动自检
    audit.py        写入门与审计回执

设计约束:
    本层只认识「schema 名」与「表名」, 不认识任何具体业务表。
    业务表的 DDL 由 seed/schema_full.sql 与 alembic 提供, 本层负责
    幂等地把它们建出来并挂上隔离策略。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.2.0"
