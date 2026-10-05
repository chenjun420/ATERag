"""溯源层: 逐属性 provenance 的落库与谱系查询。

**为什么要有这一层**: Semantica 的 :class:`~semantica.provenance.manager.ProvenanceManager`
此前**从未被调用过** —— 逐属性 provenance 只以 JSON 字段的形式躺在种子里,
没有任何可查询的谱系, Explorer 的 provenance 页面必然是空的。

这一层只做一件事: 给 ``ProvenanceManager`` 配上 PostgreSQL 后端, 让它上游的
链维护 / 校验 / 导出 / 审计逻辑直接可用。**不重写它的逻辑。**
"""

from __future__ import annotations

from aterag.provenance.pg_storage import PROV_SCHEMA, PGProvenanceStorage
from aterag.provenance.seed_loader import (
    build_manager,
    load_seed_provenance,
    load_seed_traces,
)

__all__ = [
    "PROV_SCHEMA",
    "PGProvenanceStorage",
    "build_manager",
    "load_seed_provenance",
    "load_seed_traces",
]
