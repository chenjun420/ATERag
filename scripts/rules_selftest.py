"""领域规则自验: 逐条执行 rules.yaml 的 test 块.

- derive 规则: 调用 InferenceEngine.calculate(rule_id, given), 比对 expect 数值
- constraint 规则: 由 given 构造 Turtle 数据图, 用该规则自身的 shape 做 SHACL 校验, 比对 expect.violation

用法: $env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts\\rules_selftest.py [domain]
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "src")

os.environ.setdefault("POSTGRES_DSN", "postgresql://x:x@127.0.0.1/x")
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:6333")
os.environ.setdefault("LLM_BASE", "http://x/v1")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBED_BASE", "http://x")
os.environ.setdefault("EMBED_MODEL", "x")

from aterag.config import get_settings
from aterag.inference import InferenceEngine

# test.given 的 snake_case -> SHACL 属性 (camelCase)
PS_PROP = {
    "trip": "tripValue",
    "recovery": "recoveryValue",
    "hysteresis": "hysteresis",
    "direction": "direction",
    "alarm": "alarmValue",
    "protection_trip": "protectionTrip",
    "margin": "margin",
    "diameter": "diameter",
    "spacing": "neighborSpacing",
    "rating": "currentRating",
    "need": "requiredCurrent",
    "contact_resistance": "contactResistance",
    "contact_resistance_limit": "contactResistanceLimit",
    "contact_resistance_margin": "contactResistanceMargin",
    "current_rating": "currentRating",
    "required_current": "requiredCurrent",
    "min_centers": "minCenters",
    "effective_cycle_life": "effectiveCycleLife",
    "insulation_resistance": "insulationResistance",
    "ground_resistance": "groundResistance",
    "has_esd_protection": "hasESDProtection",
    "connection_type": "connectionType",
    "dut_resistance": "dutResistance",
    "percent_gauge_rr": "percentGaugeRR",
    "priority": "priority",
    "has_test_case": "hasTestCase",
    "name": "name",
    "value": "value",
    "min": "min",
    "max": "max",
    # 批次2 DC-DC
    "measured_ripple_mvpp": "measuredRippleMvpp",
    "ripple_limit_mvpp": "rippleLimitMvpp",
    "ripple_current_actual": "rippleCurrentActual",
    "ripple_current_rating": "rippleCurrentRating",
    "peak_current": "peakCurrent",
    "sat_current": "satCurrent",
    "measured_turns_ratio": "measuredTurnsRatio",
    "nominal_turns_ratio": "nominalTurnsRatio",
    "turns_ratio_tolerance": "turnsRatioTolerance",
    "measured_deviation_pct": "measuredDeviationPct",
    "deviation_limit_pct": "deviationLimitPct",
    "measured_recovery_us": "measuredRecoveryUs",
    "recovery_limit_us": "recoveryLimitUs",
    "line_deviation_pct": "lineDeviationPct",
    "line_deviation_limit_pct": "lineDeviationLimitPct",
    "measured_overshoot_pct": "measuredOvershootPct",
    "overshoot_limit_pct": "overshootLimitPct",
    # 批次3 保护
    "turn_on_threshold": "turnOnThreshold",
    "turn_off_threshold": "turnOffThreshold",
    "protection_mode": "protectionMode",
    "requires_manual_reset": "requiresManualReset",
    "short_circuit_current": "shortCircuitCurrent",
    "current_limit": "currentLimit",
    "has_protection": "hasProtection",
    "trip_measured": "tripMeasured",
    "trip_nominal": "tripNominal",
    "trip_tolerance": "tripTolerance",
    "open_circuit_detected": "openCircuitDetected",
    "fail_safe_clamp": "failSafeClamp",
    "control_type": "controlType",
    "recovery_mode": "recoveryMode",
    "reset_clears_active_fault": "resetClearsActiveFault",
    "severity": "severity",
    "input_type": "inputType",
    "min_operating_voltage": "minOperatingVoltage",
    "supports_parallel": "supportsParallel",
    # 批次3 输出规格 / 效率功率
    "v_low_line": "vLowLine",
    "v_high_line": "vHighLine",
    "v_nominal": "vNominal",
    "measured_regulation_pct": "measuredRegulationPct",
    "regulation_limit_pct": "regulationLimitPct",
    "measured_error_pct": "measuredErrorPct",
    "accuracy_limit_pct": "accuracyLimitPct",
    "measured_sharing_deviation_pct": "measuredSharingDeviationPct",
    "sharing_deviation_limit_pct": "sharingDeviationLimitPct",
    "remaining_capacity_a": "remainingCapacityA",
    "full_load_a": "fullLoadA",
    "has_remote_sense": "hasRemoteSense",
    "measured_hold_up_ms": "measuredHoldUpMs",
    "hold_up_limit_ms": "holdUpLimitMs",
    "measured_inrush_a": "measuredInrushA",
    "inrush_limit_a": "inrushLimitA",
    "vac_rms": "vacRms",
    "p_out": "pOut",
    "p_in": "pIn",
    "judgement_margin_pct": "judgementMarginPct",
    "meter_accuracy_pct": "meterAccuracyPct",
    "measured_no_load_w": "measuredNoLoadW",
    "no_load_limit_w": "noLoadLimitW",
    "measured_pout_w": "measuredPoutW",
    "measured_pin_w": "measuredPinW",
    # 批次5 安规
    "test_voltage_v": "testVoltageV",
    "insulation_type": "insulationType",
    "has_over_current_protection": "hasOverCurrentProtection",
    "working_voltage_peak": "workingVoltagePeak",
    "actual_clearance_mm": "actualClearanceMm",
    "i_leak": "iLeak",
    "freq": "freq",
    "v_ac": "vAc",
    "connects_to": "connectsTo",
    "cap_class": "capClass",
    "fuse_i2t": "fuseI2t",
    "device_i2t": "deviceI2t",
    "measured_touch_current_ma": "measuredTouchCurrentMa",
    "touch_current_limit_ma": "touchCurrentLimitMa",
    "measurement_type": "measurementType",
    "first_step": "firstStep",
    # 批次6 遥测/遥信/遥控/校准降额
    "measured_telemetry_error_pct": "measuredTelemetryErrorPct",
    "telemetry_error_limit_pct": "telemetryErrorLimitPct",
    "sample_rate_hz": "sampleRateHz",
    "switching_frequency_hz": "switchingFrequencyHz",
    "debounce_ms": "debounceMs",
    "r25": "r25",
    "b_coeff": "bCoeff",
    "temperature_k": "temperatureK",
    "measured_temp_error_c": "measuredTempErrorC",
    "temp_error_limit_c": "tempErrorLimitC",
    "has_remote_control": "hasRemoteControl",
    "current_setpoint_max": "currentSetpointMax",
    "ocp_trip_value": "ocpTripValue",
    "setpoint_margin": "setpointMargin",
    "setpoint_readback_delta_pct": "setpointReadbackDeltaPct",
    "readback_limit_pct": "readbackLimitPct",
    "measured_command_latency_ms": "measuredCommandLatencyMs",
    "command_latency_limit_ms": "commandLatencyLimitMs",
    "measured_alarm_latency_ms": "measuredAlarmLatencyMs",
    "alarm_latency_limit_ms": "alarmLatencyLimitMs",
    "contact_type": "contactType",
    "fail_safe_state": "failSafeState",
    "comm_error_count": "commErrorCount",
    "severity_sequence": "severitySequence",
    "isolation_level": "isolationLevel",
    "main_isolation_level": "mainIsolationLevel",
    "fault_active": "faultActive",
    "sharing_fault_threshold_pct": "sharingFaultThresholdPct",
    "calibration_residual_pct": "calibrationResidualPct",
    "calibration_limit_pct": "calibrationLimitPct",
    "cal_storage": "calStorage",
    "working_voltage_v": "workingVoltageV",
    "rated_voltage_v": "ratedVoltageV",
    "junction_temp_c": "junctionTempC",
    "derated_max_junction_temp_c": "deratedMaxJunctionTempC",
    "rth_jc": "rthJc",
    "rth_cs": "rthCs",
    "rth_sa": "rthSa",
    "measured_thermal_resistance": "measuredThermalResistance",
    "thermal_resistance_limit": "thermalResistanceLimit",
    "ambient_temp_c": "ambientTempC",
    "derated_capacity_a": "deratedCapacityA",
    "rated_capacity_a": "ratedCapacityA",
    # 低线输入降额 (K-PWR-123): shape 查 ratedPowerW / lineDeratedPowerW
    "rated_power_w": "ratedPowerW",
    "line_derated_power_w": "lineDeratedPowerW",
    # 多路输出功率守恒 (K-PWR-124/125): shape 查 railPowerDeviationW / railPowerBudgetToleranceW
    "rail_power_sum_w": "railPowerSumW",
    "declared_total_output_power_w": "declaredTotalOutputPowerW",
    "rail_power_deviation_w": "railPowerDeviationW",
    "rail_power_budget_tolerance_w": "railPowerBudgetToleranceW",
}
SCOPE_CLASS = {
    "probe": "Probe",
    "measurement": "Measurement",
    "protection": "Protection",
    "limits": "Parameter",
    "coverage": "Requirement",
    "alarm": "Alarm",
    "dc-dc-test": "TransientSpec",
    "ripple": "RippleSpec",
    "transformer": "Transformer",
    "startup": "StartupSpec",
    "ovp": "ProtectionPoint",
    "uvlo": "UVLO",
    "ocp": "OCPSpec",
    "hysteresis": "Protection",
    "digital": "Product",
    "latching": "FaultSpec",
    "arbitration": "AlarmSpec",
    "reverse-polarity": "Product",
    "reverse-current": "Product",
    "accuracy": "ProtectionPoint",
    "redundancy": "RedundancySpec",
    "remote-sense": "Product",
    "inrush": "InrushSpec",
    "efficiency": "EfficiencySpec",
    "regulation": "RegulationSpec",
    "current-sharing": "CurrentSharingSpec",
    "safety": "SafetyMeasurement",
    "telemetry": "TelemetrySpec",
    "control": "ControlSpec",
    "calibration": "CalibrationSpec",
    "derating": "ThermalTest",
    # AC-DC 侧 (K-PWR-122/123): shape 的 targetClass 为 PowerTest
    "ac-dc-test": "PowerTest",
}
CLASS_OVERRIDE = {
    "K-SAF-001": "Insulation",
    "K-SAF-002": "Fixture",
    "K-SAF-003": "Fixture",
    "K-FIX-005": "Probe",
    "K-FIX-018": "Probe",
    "K-FIX-016": "MeasurementSystem",
    "K-PROT-002": "Alarm",
    "K-COV-001": "Requirement",
    "K-PWR-102": "RippleSpec",
    "K-PWR-103": "Capacitor",
    "K-PWR-104": "Inductor",
    "K-PWR-105": "Transformer",
    "K-PROT-107": "Product",
    "K-PROT-109": "Product",
    "K-PROT-110": "UVAlarm",
    "K-PWR-112": "AccuracySpec",
    "K-PWR-119": "TestEquipment",
    "K-PWR-111": "RegulationSpec",
    "K-PWR-113": "CurrentSharingSpec",
    "K-PWR-116": "InrushSpec",
    "K-PWR-120": "EfficiencySpec",
    "K-PWR-121": "EfficiencySpec",
    "K-SAF-101": "HipotSpec",
    "K-SAF-102": "TestEquipment",
    "K-SAF-103": "InsulationCoordination",
    "K-SAF-105": "YCapacitor",
    "K-SAF-106": "FuseSpec",
    "K-SAF-108": "TestEquipment",
    "K-SAF-109": "TestProcedure",
    "K-TLM-103": "DigitalInput",
    "K-TLM-111": "DigitalOutput",
    "K-CAL-103": "Capacitor",
    "K-PROT-112": "Sensor",
    "K-TLM-106": "Product",
    "K-CTL-101": "AlarmSpec",
    "K-CTL-102": "DigitalOutput",
    "K-CTL-104": "CurrentSharingSpec",
    "K-PWR-125": "MultiRailOutput",
}
# 规则级属性名覆盖 (同一 given key 在不同 shape 下对应不同属性)
PROP_OVERRIDE = {
    "K-PROT-002": {"trip": "protectionTrip"},
}
# 值为 False 的 "has_*" 布尔属性视为"不具备" -> 不生成三元组 (SHACL 校验存在性/hasValue 时需要)
ABSENT_WHEN_FALSE = ("has_test_case", "has_esd_protection")


def turtle_literal(v) -> str:
    if isinstance(v, bool):
        return f'"{str(v).lower()}"^^xsd:boolean'
    if isinstance(v, (int, float)):
        return f'"{v}"^^xsd:double'
    return '"' + str(v).replace('"', '\\"') + '"'


def build_ttl(rule_id: str, scope: str, given: dict) -> str:
    cls = CLASS_OVERRIDE.get(rule_id) or SCOPE_CLASS.get(scope, "Parameter")
    lines = [
        "@prefix ps: <https://aterag.example.org/power#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        f"ps:t a ps:{cls} ;",
    ]
    overrides = PROP_OVERRIDE.get(rule_id, {})
    props = []
    for k, v in given.items():
        if k in ABSENT_WHEN_FALSE and v is False:
            continue
        prop = overrides.get(k) or PS_PROP.get(k, k)
        props.append(f"    ps:{prop} {turtle_literal(v)}")
    if "name" not in given:
        props.insert(0, '    ps:name "test-param"')
    if not props:
        props.append("    ps:value 0")
    return "\n".join(lines) + "\n" + " ;\n".join(props) + " .\n"


def close(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= max(1e-9, abs(float(b)) * 1e-9)
    except (TypeError, ValueError):
        return a == b


def main(domain: str) -> int:
    s = get_settings()
    eng = InferenceEngine(s, domain)
    rules = eng.rules
    npass = nfail = 0
    failures: list[str] = []
    for r in rules:
        t = r.get("test")
        rid = r.get("id")
        if not t:
            failures.append(f"{rid}: 缺少 test 块")
            nfail += 1
            continue
        given = t.get("given", {}) or {}
        expect = t.get("expect", {}) or {}
        try:
            if r.get("derive"):
                res = eng.calculate(rid, dict(given))
                val = res["value"]
                ok = all(close(val, v) for v in expect.values())
                got = {k: val for k in expect}
            elif r.get("constraint"):
                from pyshacl import validate

                ttl = build_ttl(rid, r.get("scope", ""), given)
                conforms, _, _ = validate(
                    data_graph=ttl,
                    shacl_graph=r["constraint"]["shape"],
                    inference="rdfs",
                )
                viol = not conforms
                ok = viol == bool(expect.get("violation", False))
                got = {"violation": viol}
            else:
                failures.append(f"{rid}: 既无 derive 也无 constraint")
                nfail += 1
                continue
        except Exception as e:  # noqa: BLE001
            failures.append(f"{rid}: 执行异常 {e}")
            nfail += 1
            continue
        if ok:
            npass += 1
        else:
            nfail += 1
            failures.append(f"{rid}: expect={expect} got={got}")

    total = npass + nfail
    print(f"domain={domain} rules={len(rules)} selftest={npass}/{total}")
    for f in failures:
        print("  FAIL", f)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "power"))
