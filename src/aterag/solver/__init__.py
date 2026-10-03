"""L3 求解层。

    symbolic.py   量纲与数值求值 (ADR-015) —— W0 交付
    csp.py        仪器选型约束求解
    rcpsp.py       CP-SAT 资源受限项目调度
    converter.py  附录T 状态空间平均

包级不导出具体符号: 各模块的导入路径要显式 (aterag.solver.symbolic),
这样 §18.2 契约与实现的对应关系在 import 语句里就能看出来, 而不是
被 `from aterag.solver import *` 抹平。
"""

from __future__ import annotations

__all__: list[str] = []
