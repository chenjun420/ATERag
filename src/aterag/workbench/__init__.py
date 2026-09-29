"""工作台 (P1b) —— 面向评审的管理面。

只做两件事:
  * 读: 重跑抽取并组装评审快照(确定性, 不缓存)
  * 写: 经由 gate 的白名单闸写注记 + 单文件 git commit

读与写的权限面与板卡 MCP 完全分离: 产线只读, 评审可签。
"""

from aterag.workbench.gate import (
    WriteNotAllowed,
    WritePolicy,
    WriteReceipt,
    WriteRejected,
    approve_entry,
    repo_is_clean,
)
from aterag.workbench.service import ReviewTask, Workbench, WorkbenchSnapshot

__all__ = [
    "ReviewTask",
    "Workbench",
    "WorkbenchSnapshot",
    "WriteNotAllowed",
    "WritePolicy",
    "WriteReceipt",
    "WriteRejected",
    "approve_entry",
    "repo_is_clean",
]
