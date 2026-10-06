"""推理链落谱系: calculate -> l0_term.provenance。

钉住的行为:

1. ``calculate`` 给了 recorder 就**必须**记录, 记录失败整体炸 ——
   「算得出但不记录 = 没算」, 这是审计红线, 不是可选项。
2. 记录内容带输入出处链(输入 <- SR 条目), 调用方给的输入显式 ``(caller)``。
3. 不给 recorder 维持旧行为(只内存 trace) —— 明确的降级选择, 不是缺省偷懒。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "_decision_prov", ROOT / "src" / "aterag" / "inference" / "decision_prov.py"
)
assert _spec and _spec.loader
dp = importlib.util.module_from_spec(_spec)
sys.modules["_decision_prov"] = dp
_spec.loader.exec_module(dp)

_spec2 = importlib.util.spec_from_file_location(
    "_eng", ROOT / "src" / "aterag" / "inference" / "engine.py"
)
assert _spec2 and _spec2.loader
eng_mod = importlib.util.module_from_spec(_spec2)
sys.modules["_eng"] = eng_mod
_spec2.loader.exec_module(eng_mod)


class StubManager:
    """ProvenanceManager 的行为桩: 只照抄 track_entity 的调用面。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    def track_entity(self, **kw):  # noqa: ANN003
        if self.fail:
            raise RuntimeError("pg down")
        self.calls.append(kw)
        return {"entity_id": kw["entity_id"]}


class FakeSettings:
    domain_rules_dir = str(ROOT / "domain_rules")


@pytest.fixture()
def result() -> dict:
    return {
        "rule_id": "K-ELEC-001",
        "statement": "直流功率 = 电压 × 电流 (P = U × I)",
        "output": "power",
        "value": 599.4,
        "inputs": {"voltage": 54, "current": 11.1},
        "domain_layer": "power",
        "confidence": 0.95,
        "confidence_unknown": False,
        "source": {"name": "GB/T 2900.1-2008", "url": "https://openstd.samr.gov.cn/x"},
        "decision_id": "d-1",
        "input_sources": {"voltage": {"req_id": "SR-1201"}, "current": "(caller)"},
    }


class TestRecordsDecision:
    def test_entity_id_is_prefixed(self, result: dict) -> None:
        assert dp.decision_entity_id("d-1") == "dec:d-1"
        # 与种子实体 id 空间可区分 —— 种子 id 不会带这个前缀
        assert not dp.decision_entity_id("").startswith("HYST")

    def test_record_captures_audit_chain(self, result: dict) -> None:
        m = StubManager()
        eid = dp.DecisionRecorder(m).record(result)
        assert eid == "dec:d-1"
        kw = m.calls[0]
        assert kw["entity_type"] == dp.DECISION_TYPE
        # 出处可定位: url 优先于名称
        assert kw["source"] == "https://openstd.samr.gov.cn/x"
        meta = kw["metadata"]
        # 冲突消解只用 credibility —— credibility 与 confidence 同落, 且量纲
        # 是规则 yaml 的「出处可信度」(0.95), 与种子同一约定
        assert meta["credibility"] == 0.95
        assert meta["rule_id"] == "K-ELEC-001"
        # 每个输入都有出处, caller 给的显式标注而不是缺省字段消失
        assert meta["input_sources"]["voltage"] == {"req_id": "SR-1201"}
        assert meta["input_sources"]["current"] == "(caller)"
        # agent 信息: 自动化软件代理, 供审计区分「谁算的」
        assert kw["agent_type"] == "software_agent"
        assert kw["is_automated"] is True

    def test_record_failure_is_loud(self, result: dict) -> None:
        """推理不落谱系 = 审计缺口, 必须炸而不能返回没谱系的结果。"""
        m = StubManager()
        m.fail = True
        r = dp.DecisionRecorder(m)
        with pytest.raises(RuntimeError):
            r.record(result)


class TestEngineWiring:
    def _engine(self, facts: dict | None) -> eng_mod.InferenceEngine:
        return eng_mod.InferenceEngine(FakeSettings(), "power", model_facts=facts or {})

    def test_input_sources_map_every_var(self) -> dict:
        """每个表达式输入都得到出处: 事实里的查 `_provenance`,
        调用方的标 `(caller)` —— 不留空。"""
        e = self._engine({"power": 600, "_provenance": {"power": {"req_id": "SR-9"}}})
        got = e._input_sources(["power", "voltage"])
        assert got["power"] == {"req_id": "SR-9"}
        assert got["voltage"] == "(caller)"

    def test_calculate_without_recorder_stays_old(self) -> dict:
        """不给 recorder: 结果照旧(无 provenance_entity_id), 不缺省记账。"""
        e = self._engine({"voltage": 54, "current": 11.1})
        got = e.calculate("power", {"voltage": 54, "current": 11.1})
        assert "provenance_entity_id" not in got

    def test_calculate_with_recorder_persists(self, result: dict) -> None:
        """给了 recorder: calculate 返回里带 entity_id, 且桩上真有记录。"""
        m = StubManager()
        e = self._engine({"voltage": 54, "current": 11.1})
        e._recorder = dp.DecisionRecorder(m)
        got = e.calculate("power", {"voltage": 54, "current": 11.1})
        assert got["provenance_entity_id"].startswith("dec:")
        assert len(m.calls) == 1

    def test_calculate_recorder_failure_breaks_calculation(self) -> None:
        """记录失败 = calculate 整体失败 —— 不是降级成「没谱系的计算」。"""
        m = StubManager()
        m.fail = True
        e = self._engine({"voltage": 54, "current": 11.1})
        e._recorder = dp.DecisionRecorder(m)
        with pytest.raises(RuntimeError):
            e.calculate("power", {"voltage": 54, "current": 11.1})
