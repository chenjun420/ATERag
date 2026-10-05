"""ATERag 对外 HTTP 门面。

**只有一个模块, 且刻意只有这一个**: :mod:`aterag.api.explorer` 提供
``/aterag/*``(健康 + 图谱概览)并在其上挂载 Semantica Explorer UI。

仓库里另有一个 ``aterag/extract/api.py`` —— 那是**抽取层**的纯函数接口
(解析/组装规格书), 名字里的 api 与 HTTP 无关, 且它明确禁止 import
``mcp_server`` 与 ``rag.service``。两者无依赖关系。
"""

from __future__ import annotations

__all__ = ["explorer"]
