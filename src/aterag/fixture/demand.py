"""工装能力需求推导 (任务③ 的可判定部分)。

## 需求 ≠ 设计

本模块只回答「工装与仪器**必须能做什么**」, 不产出 ``fixture`` /
``fixture_channel_map`` 行。那两张表里, ``fixture_channel_map`` 承载的只是**信号接线**
类通道(干接点/电平/模拟量/电流环), 而**电气测量判据(TEST/PROT)的物理通路根本
不进这张表** —— 探头、AC 源、电子负载、故障注入的通路属于工装结构与工位布线的
设计决策, 属于产品侧, 不是从判据推出来的。

**信号类条目(YX/YC/YK/YT)是另一回事**: 遥测/遥控/遥信大多是**总线功能** ——
一个通信端口承载 N 个逻辑点位, 工装接一条通信通道 + 协议分析仪即可,
不需要给每个遥控点位布一条物理线。``ck_exactly_one_target`` 要求每通道行恰好
映射一个点位, 与「一个端口多点位」并不冲突(每点位一行逻辑通道)。要落这些行
需要 ``yx_point`` / ``yc_point`` / ``yk_command`` / ``yt_parameter`` /
``protection_setting`` 有数据(信号表未结构化, 实测 0 行), 凭空造点位等于伪造
产品数据。所以本模块产出「需求 + 缺口清单」, 由人补设计。

## 三条纪律

1. **每条需求都带出处**: ``demanded_by`` 记 sr_id, 否则「需要四线制取样」是一句
   无源的话, 评审时无从核对(红线 5)。
2. **未人审的条件只算提示**: 含 draft 子句的判据满足的是「业界提案」而非已批准
   判据, 需求强度不同, 用 ``provisional`` 标出而不是混进硬需求 —— 否则「要买 8 台
   仪器」可能只是因为 8 条提案都没签字。
3. **单一实现**: ``kind -> 仪器能力`` 的映射表由本模块独占, 落库层
   (:func:`persist_requirements._instrument_need`) 也从这里取。两处各拼一次必然漂,
   而漂掉的那份不报错、只是让工装软件少提一项需求。

## 输入

吃**落库行的形状**(``spec`` / ``condition_vector`` / ``signal_type`` / ``sr_id`` …),
而不是 ``TestCondition`` —— 因为这正是从 PG 读回来的形状, 于是「对已落库的 95 行
重算」与「对新抽的结果推导」走同一条代码路径。

不 import ``persist_requirements``: 它要从这里取映射表, 反向依赖会成环。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: ``kind -> 需要的仪器能力类别``。**本模块独占**: 落库层也从这里取。
#:
#: 这里给的是「能力」不是「型号」: 有电子负载不等于有 CR/CP 模式, 有万用表不等于
#: 能同步采样电压电流做效率。选型要靠 ``instrument_ledger``(在册台账), 而那张表
#: 实测 0 行 —— 没有台账就报「需要能做什么」, 不假装已经选好了。
CAPABILITY_BY_KIND: Mapping[str, tuple[str, ...]] = {
    "input_voltage": ("programmable_ac_source",),
    "input_frequency": ("programmable_ac_source",),
    "load": ("programmable_dc_load",),
    # CR/CP 恒流恒压模式只有电子负载有, 独立电源做不到
    "test_mode": ("programmable_dc_load",),
    "output_voltage": ("dmm",),
    "output_current": ("dmm",),
    "output_power": ("power_analyzer",),
    "ripple": ("oscilloscope", "bandwidth_limited_probe"),
    "timing": ("oscilloscope",),
    "efficiency": ("power_analyzer", "four_wire_sense"),
    "power_factor": ("power_analyzer", "four_wire_sense"),
    "signal_state": ("protocol_analyzer",),
    "telemetry_value": ("protocol_analyzer",),
    "protection_action": ("fault_injection_path", "protection_tester"),
    "thermal": ("thermal_chamber",),
    "command": ("protocol_analyzer",),
    "cap_load": ("capacitor_bank",),
    "fault_stimulus": ("fault_injection_path",),
    # ``measurement_setup`` 刻意**不**映射: 它回答的是「怎么测」(带宽限带、四线制
    # 取样点、并联电容), 对应的是工装/探头特性而非仪器类别。硬给它造一个
    # ``measurement_setup_defined`` 之类的伪能力名, 会让采购清单里出现无法下单的项。
    # 测法类需求由 :func:`derive_fixture_type_demand` 与工装层按结构化键处理。
}

#: 数字单位 -> 该单位要求的能力类。用于从 ``spec`` 反推**量程**而不是「有没有仪器」。
#:
#: 只收这些单位是因为只有它们直接给出仪器选型边界(量程/带宽/时间分辨率);
#: 温度系数(``%/℃``)、效率(``%``)这类不构成选型约束。
UNIT_CAPABILITY: Mapping[str, tuple[str, ...]] = {
    "Vac": ("programmable_ac_source",),
    "Vac/Vdc": ("programmable_ac_source",),
    "A": ("programmable_dc_load",),
    "W": ("power_analyzer",),
    "V": ("dmm",),
    "mV": ("oscilloscope",),
    "us": ("oscilloscope",),
    "ms": ("oscilloscope",),
    "mS": ("oscilloscope",),
    "Hz": ("programmable_ac_source",),
}


@dataclass(frozen=True)
class DemandItem:
    """一条能力需求。

    ``provisional=True`` 表示其依据里**含未人审的业界提案**: 该需求成立与否取决于
    提案签字结果, 不是已批准判据的硬要求。
    """

    capability: str
    demanded_by: tuple[str, ...] = ()
    #: 硬需求指向已签/规则来源; provisional 指向含 draft 子句的判据。
    approved_by: tuple[str, ...] = ()
    provisional_by: tuple[str, ...] = ()
    detail: str = ""

    @property
    def provisional_only(self) -> bool:
        return not self.approved_by and bool(self.provisional_by)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "demanded_by": sorted(self.demanded_by),
            "approved_by": sorted(self.approved_by),
            "provisional_by": sorted(self.provisional_by),
            "provisional_only": self.provisional_only,
            "detail": self.detail,
        }


def _clauses(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    vec = row.get("condition_vector") or {}
    return [c for side in ("approved", "draft") for c in (vec.get(side) or [])]


def _is_draft_only(row: Mapping[str, Any]) -> bool:
    """该行是否**只有** draft 子句(没有任何已批准依据)。"""
    vec = row.get("condition_vector") or {}
    return not (vec.get("approved") or []) and bool(vec.get("draft") or [])


def derive_capability_demand(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[DemandItem, ...]:
    """判据行 -> 仪器/工装能力需求。

    两路证据并集: 条件子句的 ``kind``(说明**怎么测**)与 ``spec`` 的单位(说明
    **量程**)。只走一路会漏 —— PA601 实测 ``kind`` 侧推出 7 类能力, 而 ``spec``
    侧的 ``Vac`` 到 315V、``A`` 到 18A 才是选型边界。
    """
    approved: dict[str, set[str]] = {}
    provisional: dict[str, set[str]] = {}
    for row in rows:
        sr_id = str(row.get("sr_id") or "")
        if not sr_id:
            continue
        caps: set[str] = set()
        for cl in _clauses(row):
            caps.update(CAPABILITY_BY_KIND.get(str(cl.get("kind") or ""), ()))
        unit = str((row.get("spec") or {}).get("unit") or "")
        caps.update(UNIT_CAPABILITY.get(unit, ()))
        if not caps:
            continue
        bucket = provisional if _is_draft_only(row) else approved
        for c in caps:
            bucket.setdefault(c, set()).add(sr_id)
    out: list[DemandItem] = []
    for cap in sorted(set(approved) | set(provisional)):
        a = frozenset(approved.get(cap, ()))
        p = frozenset(provisional.get(cap, ()))
        # 空集存成 () 而不是 frozenset(): 调用方要能 ``not item.approved_by``
        # 判断「有没有已批准依据」, 空的 frozenset 与 () 语义同而在 == 上不同
        out.append(DemandItem(capability=cap, demanded_by=tuple(sorted(a | p)),
                              approved_by=tuple(sorted(a)),
                              provisional_by=tuple(sorted(p))))
    return tuple(out)


@dataclass(frozen=True)
class RailDemand:
    """一条电压轨的通道需求。

    ``sense_channels`` 只数**测量**通道(电压采样 / 电流采样), 不含负载切换与
    保护注入 —— 后者是工装的动作通路, 由 ``fixture`` 的 ``fixture_type`` 表达,
    混进通道数会让工装多算物理端口。
    """

    rail: str
    sense_channels: int
    demanded_by: tuple[str, ...] = ()
    needs_switching: bool = False
    needs_fault_injection: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rail": self.rail,
            "sense_channels": self.sense_channels,
            "demanded_by": sorted(self.demanded_by),
            "needs_switching": self.needs_switching,
            "needs_fault_injection": self.needs_fault_injection,
        }


#: 电压轨归一。``-54V`` / ``-54`` / ``54V`` 指同一条轨; 归不到轨的走「(整机)」。
_WS = str.maketrans({c: None for c in " \t　"})


def normalize_rail(rail: str | None) -> str:
    """电压轨文本 -> 归一后的轨名。

    空/无轨的判据归 ``(整机)`` —— 它们测的是整机的输入或与轨无关的量(如待机功耗),
    强行挂到某条轨上会让那一轨的通道需求虚高。
    """
    r = (rail or "").translate(_WS)
    if not r or r in {"-", "—", "/"}:
        return "(整机)"
    if r.lower().endswith("v"):
        r = r[:-1]
    return r or "(整机)"


#: 会产生**测量**通道的 kind: 电压采样 / 电流采样。
_SENSE_KINDS: Mapping[str, str] = {
    "output_voltage": "voltage",
    "output_current": "current",
    "ripple": "voltage",
}


def derive_rail_channel_demand(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[RailDemand, ...]:
    """判据行 -> 每条轨的通道需求。

    以 ``spec.rail`` 为准(与 eid 档位后缀同源), 而不是靠 ``source_ref.rail`` ——
    后者在未标注轨时是空串, 而 ``spec`` 里至少保留了原判据的单位信息。
    """
    acc: dict[str, dict[str, Any]] = {}
    for row in rows:
        spec = row.get("spec") or {}
        rail = normalize_rail(str(spec.get("rail") or ""))
        sr_id = str(row.get("sr_id") or "")
        entry = acc.setdefault(rail, {"senses": set(), "by": set(),
                                      "switch": False, "fault": False})
        if sr_id:
            entry["by"].add(sr_id)
        for cl in _clauses(row):
            kind = str(cl.get("kind") or "")
            if kind in _SENSE_KINDS:
                entry["senses"].add(_SENSE_KINDS[kind])
            if kind == "load":
                entry["switch"] = True
            if kind in ("fault_stimulus", "protection_action"):
                entry["fault"] = True
    # 无轨判据的采样要求是**底座**, 传播到每一条轨: PA601 实测, 输入域判据
    # (没写轨) 的输出子句是「各路输出电压应落在其额定范围」——"各路"意味着
    # 每条输出轨都得测, 只按该行自身 kind 计数会得出「(整机) 1 通道」的低估。
    # 把这些要求并进各轨的 senses 后, (整机) 桶仍保留自身数值, 而轨级消费方
    # 看到的是两者并集 —— 某轨缺哪类采样就真的会缺, 而不是靠别的判据恰好补上。
    floor = acc.get("(整机)")
    for rail, e in acc.items():
        if rail != "(整机)" and floor:
            e["senses"] |= floor["senses"]
    return tuple(
        RailDemand(rail=rail, sense_channels=len(e["senses"]),
                   demanded_by=frozenset(e["by"]),
                   needs_switching=bool(e["switch"]),
                   needs_fault_injection=bool(e["fault"]))
        for rail, e in sorted(acc.items())
    )


#: ``fixture.fixture_type`` 的合法取值(对齐 DDL CHECK)。用于把能力需求映射到
#: 「该有哪些工装形态」, 而不是凭空发明工装名。
FIXTURE_TYPES: tuple[str, ...] = (
    "load_board", "relay_matrix", "adapter", "fault_injection",
    "load_box", "safety_fixture", "emc_fixture", "thermal_adapter",
    "fixture_adapter",
)

#: 能力类别 -> 需要的工装形态。**只列有据可依的**: 形态是产品侧决策, 这里只指出
#: 「某项能力要求存在对应形态的工装」, 不指定型号/通道数/品牌。
_CAPABILITY_FIXTURE_TYPES: Mapping[str, tuple[str, ...]] = {
    "programmable_dc_load": ("load_box",),
    "capacitor_bank": ("load_box",),
    "fault_injection_path": ("fault_injection",),
    "thermal_chamber": ("thermal_adapter",),
}


def derive_fixture_type_demand(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    """判据行 -> 需要的工装形态。

    信号类能力(protocol_analyzer)要求的是**仪器**不是工装, 故不列形态 ——
    把仪器需求误算成工装形态会让工装采购多背一笔。
    """
    caps = {d.capability for d in derive_capability_demand(rows)}
    wanted: set[str] = set()
    for cap in caps:
        wanted.update(_CAPABILITY_FIXTURE_TYPES.get(cap, ()))
    # 每条带轨判据都要按轨分开接线 -> 需要转接/适配
    if any(d.rail != "(整机)" and d.sense_channels for d in derive_rail_channel_demand(rows)):
        wanted.add("adapter")
    return tuple(t for t in FIXTURE_TYPES if t in wanted)


@dataclass(frozen=True)
class InstrumentRange:
    """一台仪器要覆盖的量程边界, 及其出处。"""

    capability: str
    unit: str
    low: float | None
    high: float | None
    demanded_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "unit": self.unit,
                "low": self.low, "high": self.high,
                "demanded_by": sorted(self.demanded_by)}


def derive_instrument_ranges(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[InstrumentRange, ...]:
    """判据行 -> 仪器量程需求(按单位归并)。

    只取有**数值**的 ``min``/``max``/``typ``。空值不参与: ``spec`` 里
    ``rail=''`` / ``unit=''`` 是常态, 拿它们当量程会得出「0~0 V」这种看似有界
    实则无信息的结论。
    """
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        spec = row.get("spec") or {}
        unit = str(spec.get("unit") or "")
        if not unit:
            continue
        caps = UNIT_CAPABILITY.get(unit)
        if not caps:
            continue
        vals = [spec.get(k) for k in ("min", "typ", "max")]
        nums = [float(v) for v in vals if isinstance(v, (int, float))]
        if not nums:
            continue
        sr_id = str(row.get("sr_id") or "")
        for cap in caps:
            e = acc.setdefault((cap, unit), {"lo": None, "hi": None, "by": set()})
            e["lo"] = min(nums) if e["lo"] is None else min(e["lo"], min(nums))
            e["hi"] = max(nums) if e["hi"] is None else max(e["hi"], max(nums))
            if sr_id:
                e["by"].add(sr_id)
    return tuple(
        InstrumentRange(capability=cap, unit=unit, low=e["lo"], high=e["hi"],
                        demanded_by=frozenset(e["by"]))
        for (cap, unit), e in sorted(acc.items())
    )


__all__ = [
    "CAPABILITY_BY_KIND",
    "UNIT_CAPABILITY",
    "FIXTURE_TYPES",
    "DemandItem",
    "RailDemand",
    "InstrumentRange",
    "derive_capability_demand",
    "derive_fixture_type_demand",
    "derive_instrument_ranges",
    "derive_rail_channel_demand",
    "normalize_rail",
]
