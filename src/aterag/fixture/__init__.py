"""L6 工装与工位层 (W6 交付)。

计划模块 (V6.0 §18.1.3):
    registry.py   治具/工位/仪器能力登记
    channelmap.py 通道映射
    poka.py       防错 (P13~P18)
    match.py      六项校核与 gap_report

状态: 未实现。

与 ATEStudio 的关系: 本层是 A-W6, 必须先于 B-01 (bundle 契约演进) 定稿,
否则「通道」概念会在两个仓库各自演化出两套定义。§18.10 注 4 明确警告过。
"""

from __future__ import annotations

__all__: list[str] = []
