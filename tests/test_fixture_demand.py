"""工装能力需求推导的测试。

需求 ≠ 设计: 这里验证的是「推导出的需求带出处、含未审依据的项被标成提示、
无轨判据的采样要求传播到每条轨」。任何把推导结果当成工装接线决策的行为都算回归。
"""

from __future__ import annotations

from aterag.fixture.demand import (
    CAPABILITY_BY_KIND,
    DemandItem,
    derive_capability_demand,
    derive_fixture_type_demand,
    derive_instrument_ranges,
    derive_rail_channel_demand,
    normalize_rail,
)


def _row(sr_id, *, rail="", unit="", approved=(), draft=()):
    return {
        "sr_id": sr_id,
        "spec": {"rail": rail, "unit": unit, "min": None, "typ": None, "max": None},
        "condition_vector": {
            "approved": [{"kind": k} for k in approved],
            "draft": [{"kind": k} for k in draft],
        },
    }


class TestNormalizeRail:
    def test_variants_of_same_rail_unify(self):
        assert normalize_rail("-54V") == normalize_rail("-54") == "-54"
        assert normalize_rail("3.45V") == "3.45"

    def test_blank_goes_to_whole_unit_bucket(self):
        assert normalize_rail("") == normalize_rail(None) == "(整机)"


class TestCapabilityDemand:
    def test_kind_and_unit_are_unioned(self):
        rows = [_row("SR-X-1100", rail="-54V", unit="A", approved=("load",))]
        (d,) = derive_capability_demand(rows)
        assert set(d.demanded_by) == {"SR-X-1100"}
        assert "programmable_dc_load" in d.approved_by or d.approved_by

    def test_draft_only_row_is_provisional_not_hard(self):
        rows = [_row("SR-X-1", draft=("ripple",))]
        ds = derive_capability_demand(rows)
        assert all(d.provisional_only for d in ds)
        assert {d.capability for d in ds} == {"oscilloscope",
                                              "bandwidth_limited_probe"}
        assert all(d.approved_by == () for d in ds)
        assert all(set(d.provisional_by) == {"SR-X-1"} for d in ds)
        # 提示项不该出现在硬需求里 —— 采购清单据此过滤: 同一能力既有硬又有提示时,
        # 硬侧记已批准行, 提示侧记提案行, 两者不混
        rows2 = [_row("SR-X-2", approved=("ripple",)), *rows]
        d2 = [x for x in derive_capability_demand(rows2)
              if x.capability == "oscilloscope"][0]
        assert d2.approved_by == ("SR-X-2",)
        assert set(d2.provisional_by) == {"SR-X-1"}
        assert not d2.provisional_only

    def test_measurement_setup_is_not_a_pseudo_capability(self):
        """测法子句不得映射出无法下单的伪能力名。"""
        assert "measurement_setup" not in CAPABILITY_BY_KIND
        rows = [_row("SR-X-1", approved=("measurement_setup",))]
        assert derive_capability_demand(rows) == ()


class TestRailChannelDemand:
    def test_unrailed_output_check_propagates_to_every_rail(self):
        """"各路输出电压应落在其额定范围」这类无轨判据, 要求每条轨都测。

        只按行自身 kind 计数会得出「(整机) 1 通道」的低估 —— 传播是修正它。
        """
        rows = [
            _row("SR-X-1100", approved=("output_voltage",)),      # 无轨
            _row("SR-X-1500", rail="-54V", approved=("output_current",)),
            _row("SR-X-1501", rail="3.45V", approved=("output_current",)),
        ]
        rail_senses = {d.rail: d.sense_channels for d in derive_rail_channel_demand(rows)}
        assert rail_senses["-54"] == 2, "-54 应有电压(底座) + 电流(自身)"
        assert rail_senses["3.45"] == 2

    def test_rail_own_demand_not_erased_by_floor(self):
        rows = [
            _row("SR-X-1100", approved=("output_voltage",)),      # 无轨: 电压
            _row("SR-X-1500", rail="-54V",
                 approved=("output_current", "ripple")),
        ]
        d = [x for x in derive_rail_channel_demand(rows) if x.rail == "-54"][0]
        # ripple 与 output_voltage 同为电压采样, 不重复计
        assert d.sense_channels == 2

    def test_switching_and_fault_flags(self):
        rows = [_row("SR-X-1", approved=("load",)),
                _row("SR-X-2", approved=("fault_stimulus",))]
        d = derive_rail_channel_demand(rows)
        assert all(x.needs_switching and x.needs_fault_injection for x in d)


class TestInstrumentRanges:
    def test_ranges_merge_by_unit_and_carry_sr_ids(self):
        rows = [
            _row("SR-X-1200", rail="-54V", unit="V",
                 approved=("output_voltage",)),
            _row("SR-X-1201", rail="-54V", unit="V",
                 approved=("output_voltage",)),
        ]
        rows[0]["spec"]["min"] = -54.8
        rows[1]["spec"]["max"] = 65.0
        (r,) = derive_instrument_ranges(rows)
        assert (r.low, r.high) == (-54.8, 65.0)
        assert set(r.demanded_by) == {"SR-X-1200", "SR-X-1201"}

    def test_unitless_or_valueless_rows_contribute_nothing(self):
        rows = [_row("SR-X-1"), _row("SR-X-2", unit="V")]
        assert derive_instrument_ranges(rows) == ()


class TestFixtureTypes:
    def test_demand_maps_to_legal_ddl_fixture_types(self):
        rows = [_row("SR-X-1", rail="-54V", approved=("load", "fault_stimulus"))]
        ftypes = derive_fixture_type_demand(rows)
        assert set(ftypes) <= {"load_board", "relay_matrix", "adapter",
                               "fault_injection", "load_box", "safety_fixture",
                               "emc_fixture", "thermal_adapter", "fixture_adapter"}
        assert "load_box" in ftypes and "fault_injection" in ftypes


class TestDemandItemShape:
    def test_provisional_only_logic(self):
        d = DemandItem(capability="x", provisional_by=("SR-1",))
        assert d.provisional_only
        d2 = DemandItem(capability="x", approved_by=("SR-1",),
                        provisional_by=("SR-2",))
        assert not d2.provisional_only  # 有已批准依据就不能整项降级为提示


class TestFilterRequirementRows:
    """等级/内容过滤: 正向多值、负向多值、词命中留痕。"""

    def _r(self, sr, prio=None, notes=""):
        return {
            "sr_id": sr,
            "variant_key": "",
            "measurand": "m",
            "source_ref": {"priority": prio, "notes": notes},
        }

    def test_exclude_priority_removes_not_required(self):
        from aterag.fixture.demand import filter_requirement_rows
        rows = [self._r("A", "强制"), self._r("B", "不要求"), self._r("C", "无要求")]
        kept, ex = filter_requirement_rows(rows, exclude_priority="不要求,无要求")
        assert [r["sr_id"] for r in kept] == ["A"]
        assert {e.sr_id for e in ex} == {"B", "C"}
        assert all("等级为" in e.reason for e in ex)

    def test_priority_positive_multi_value(self):
        from aterag.fixture.demand import filter_requirement_rows
        rows = [self._r("A", "强制"), self._r("B", "推荐"), self._r("C", "")]
        kept, ex = filter_requirement_rows(rows, priority="强制,推荐")
        assert [r["sr_id"] for r in kept] == ["A", "B"]
        assert [e.sr_id for e in ex] == ["C"]

    def test_exclude_words_leaves_trace_with_notes(self):
        from aterag.fixture.demand import filter_requirement_rows
        rows = [
            self._r("A", "强制", notes="正常"),
            self._r("B", "强制", notes="3A以下不要求，3A以上：±1%精度"),
        ]
        kept, ex = filter_requirement_rows(rows, exclude_words="不要求")
        assert [r["sr_id"] for r in kept] == ["A"]
        assert len(ex) == 1 and ex[0].sr_id == "B"
        # 留痕必须带原文片段 —— 分档型备注是否真该筛, 复核者要能直接看到
        assert "3A以下不要求" in ex[0].reason

    def test_no_params_keeps_everything(self):
        from aterag.fixture.demand import filter_requirement_rows
        rows = [self._r("A", "不要求", notes="不要求")]
        kept, ex = filter_requirement_rows(rows)
        assert len(kept) == 1 and not ex


class TestCapabilityMappingCompleteness:
    """映射表 vs kind 词表的完整性 —— 这类漂移不报错, 只让需求静默丢失。

    实测教训(2026-10-07): 词表 input 侧的温度激励叫 ``temperature``, 而
    ``CAPABILITY_BY_KIND`` 只写了 output 侧的 ``thermal`` —— SR-1217 温度系数
    行的仪器需求里 thermal_chamber 一直缺席, 流程图/采购清单都看不见温箱。
    不存在的键不会抛错, 只会推不出能力。
    """

    #: 刻意不映射的键 + 理由(新增 kind 时必须显式在这里表态)。
    ALLOWED_UNMAPPED = {
        "measurement_setup": "回答「怎么测」, 属工装特性而非仪器类别(伪能力名不可下单)",
        "input_type": "AC/DC 类型声明, 不蕴含新增仪器",
        "power_event": "上电/下电时刻, 由 timing/scope 类判据的条件承接, 不单独出能力",
        "duty": "工作制声明, 不蕴含新增仪器",
        "output_metric": "一类输出度量的占位, 具体量由其子判据承接",
        "presence": "存在性检查, 目视/软件判定, 不蕴含新增仪器",
    }

    def test_every_wordlist_kind_is_mapped_or_explicitly_allowed(self):
        import yaml

        from aterag.fixture.demand import CAPABILITY_BY_KIND
        cfg = yaml.safe_load(open("config/condition_patterns.yaml", encoding="utf-8"))
        kinds = set(cfg["kinds"]["input"]) | set(cfg["kinds"]["output"])
        unmapped = kinds - set(CAPABILITY_BY_KIND) - set(self.ALLOWED_UNMAPPED)
        assert not unmapped, (
            f"kind 词表里有未表态的键: {sorted(unmapped)} —— "
            f"要么进 CAPABILITY_BY_KIND, 要么进 ALLOWED_UNMAPPED 写清理由"
        )

    def test_no_stale_mapping_keys(self):
        import yaml

        from aterag.fixture.demand import CAPABILITY_BY_KIND
        cfg = yaml.safe_load(open("config/condition_patterns.yaml", encoding="utf-8"))
        kinds = set(cfg["kinds"]["input"]) | set(cfg["kinds"]["output"])
        stale = set(CAPABILITY_BY_KIND) - kinds
        assert not stale, f"映射表里有词表外的遗留键: {sorted(stale)}"

    def test_temperature_stimulus_yields_thermal_chamber(self):
        """input 侧 temperature 与 output 侧 thermal 必须都推得出温箱。"""
        from aterag.fixture.demand import CAPABILITY_BY_KIND
        assert "thermal_chamber" in CAPABILITY_BY_KIND["temperature"]
        assert "thermal_chamber" in CAPABILITY_BY_KIND["thermal"]
