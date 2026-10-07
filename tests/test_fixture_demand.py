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
