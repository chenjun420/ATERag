"""L6 工装与工位层 (W6 交付)。

已实现:
    demand.py     工装/仪器**能力需求**推导(从已落库的 test_requirement 行)

计划模块 (V6.0 §18.1.3):
    registry.py   治具/工位/仪器能力登记
    channelmap.py 通道映射
    poka.py       防错 (P13~P18)
    match.py      六项校核与 gap_report

为什么只做了 demand.py
----------------------
``channelmap`` / ``registry`` 依赖**接线决策**: 哪个物理通道接哪个点位、工装形态选
哪个、通道数多少。但要分清 ``fixture_channel_map`` 管的是谁:

- 它的 ``signal_type`` CHECK 闭集是干接点/电平/模拟量/电流环 —— 只承载**信号接线**
  类通道。电气测量判据(TEST/PROT)的物理通路(探头、AC 源、电子负载、故障注入)
  **不在这张表里**, 不受点位表影响。
- ``ck_exactly_one_target``(P13)只要求**信号类通道行**每行恰好映射一个结构化点位。
  遥测/遥控/遥信多为**总线功能**: 一个通信端口承载 N 个逻辑点位, 工装接的是一条
  通信通道, 每个点位各记一行逻辑通道即可满足约束。
- 因此点位五张表实测 0 行**只阻塞信号类通道的映射**(PA601 计 YX 17 + YC 8 行,
  含本该是 YK/YT 的 3 条遥控遥调), 不阻塞其余 TEST 55 / PROT 15 行的工装设计。
  造点位数据仍属伪造, 所以缺口如实列出交产品侧。

所以先做「需求」这一侧: 它只回答「工装与仪器必须能做什么」, 每条需求带 sr_id 出处,
并把缺口列出来交产品侧决策。**需求 ≠ 设计**, 两者分开才不至于让推导结果被当成设计。

与 ATEStudio 的关系: 本层是 A-W6, 必须先于 B-01 (bundle 契约演进) 定稿,
否则「通道」概念会在两个仓库各自演化出两套定义。§18.10 注 4 明确警告过。
"""

from __future__ import annotations

from aterag.fixture.demand import (
    DemandItem,
    InstrumentRange,
    RailDemand,
    derive_capability_demand,
    derive_fixture_type_demand,
    derive_instrument_ranges,
    derive_rail_channel_demand,
)

__all__ = [
    "DemandItem",
    "InstrumentRange",
    "RailDemand",
    "derive_capability_demand",
    "derive_fixture_type_demand",
    "derive_instrument_ranges",
    "derive_rail_channel_demand",
]
