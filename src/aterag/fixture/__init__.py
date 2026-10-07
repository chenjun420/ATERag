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
哪个、通道数多少。而 ``fixture_channel_map`` 的 ``ck_exactly_one_target`` 要求每通道
恰好映射一个点位, 点位来自 ``yx_point`` / ``yc_point`` / ``yk_command`` /
``yt_parameter`` / ``protection_setting`` —— 这五张表实测**全是 0 行**。没有点位就
在物理上无法绑定通道, 凭空造点位等于伪造产品数据。

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
