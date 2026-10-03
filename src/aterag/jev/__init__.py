"""L4 JEV 判定层 (W4 交付)。

计划模块 (V6.0 §18.1.3):
    router.py        阈值按型号分档
    gate.py          门禁判定
    calibration.py   阈值校准
    mcp.py           MCP 工具集 (从旧 mcp_server/server.py 迁移并加鉴权)

状态: 未实现。现有 src/aterag/mcp_server/server.py 仍在服务, 17 个工具,
无鉴权且绑 0.0.0.0:8080 —— 鉴权缺失是 G-01 的内容。
"""

from __future__ import annotations

__all__: list[str] = []
