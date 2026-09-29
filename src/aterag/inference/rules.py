"""领域规则加载器: domain_rules/{common,domain}/rules.yaml -> 可执行规则."""
from __future__ import annotations

import functools
from pathlib import Path

import yaml


@functools.lru_cache(maxsize=32)
def load_domain_rules(rules_dir: str, domain: str) -> tuple[list[dict], list[dict]]:
    """加载 common + 指定域的规则包 (进程内缓存, 文件变更时重启或手动清除)。"""
    base = Path(rules_dir)
    all_rules: list[dict] = []
    all_shapes: list[str] = []
    for sub in ("common", domain):
        d = base / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("*.yaml")):
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for rule in data.get("rules", []):
                rule["_domain"] = sub
                # YAML 日期 -> ISO 字符串 (JSON 可序列化)
                src = rule.get("source")
                if isinstance(src, dict) and hasattr(src.get("retrieved"), "isoformat"):
                    src["retrieved"] = src["retrieved"].isoformat()
                all_rules.append(rule)
                shape = (rule.get("constraint") or {}).get("shape")
                if shape:
                    all_shapes.append(shape)
    return all_rules, all_shapes


def find_rule(rules: list[dict], rule_id: str | None, formula_type: str | None) -> dict | None:
    if rule_id:
        for r in rules:
            if r.get("id") == rule_id:
                return r
        return None
    if formula_type:
        for r in rules:
            if r.get("id", "").endswith(formula_type.upper()) or r.get("id") == formula_type:
                return r
    return None


def formula_type_to_rule_id(formula_type: str) -> str:
    """formula_type (MCP 入参) -> 规则 ID。"""
    mapping = {
        "power": "K-ELEC-001",
        "efficiency": "K-PWR-001",
        "loss": "K-PWR-002",
        "input_power": "K-PWR-003",
        "duty_cycle": "K-PWR-004",
        "esr_loss": "K-PWR-005",
        "ohm_voltage": "K-ELEC-002",
        "ohm_current": "K-ELEC-003",
        "resistor_power_i2r": "K-ELEC-004",
        "resistor_power_u2r": "K-ELEC-005",
        "power_factor": "K-AC-001",
        "apparent_power": "K-AC-002",
        "reactive_power": "K-AC-003",
        "temp_rise": "K-THM-001",
        "junction_temp": "K-THM-002",
        "cap_life_factor": "K-THM-003",
        "tolerance": "K-FIX-002",
        "rss_tolerance": "K-FIX-017",
        "worst_case_tolerance": "K-FIX-017",
        "fixture_precision": "K-FIX-001",
        "probe_selection": "K-FIX-004",
        "channel_count": "K-FIX-006",
        "probe_life": "K-FIX-007",
        "probe_required_life": "K-FIX-014",
        "required_resolution": "K-FIX-015",
        "kelvin_measured_resistance": "K-FIX-009",
        "effective_clearance": "K-FIX-012",
        "availability": "K-REL-001",
        # 批次2/3: DC-DC 与输出规格
        "output_ripple": "K-PWR-101",
        "line_regulation_pct": "K-PWR-110",
        "efficiency_pct": "K-PWR-118",
        "rectified_bus_voltage": "K-PWR-117",
        "derated_output_current": "K-PWR-122",
        "required_bulk_capacitance": "K-PWR-115",
        "ovp_trip_voltage": "K-PROT-101",
        "min_hysteresis": "K-PROT-108",
        # 批次5/6: 安规、遥测、热设计
        "max_y_capacitance": "K-SAF-104",
        "ntc_resistance": "K-TLM-104",
        "total_thermal_resistance": "K-CAL-105",
    }
    return mapping.get(formula_type, formula_type)
