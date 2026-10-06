"""决策解释投影: 谱系行 -> 上游 Explanation 结构 + 中文审计文本。

钉住的行为:

1. **出处链进结构**: 每个输入一行 ``名 = 值 <- SR-xxxx``; 调用方给的输入
   是 ``(caller)``, 不留白。
2. **不顶格**: 出处可信度未标注 -> 结构里 0.0 + ``credibility_unknown``
   标记 + 文本「未标注」, 任何一处都不显示 1.0。
3. **确定性**: 同一条记录两次投影内容相同 (explanation_id 不掺时间戳),
   解释能被 diff 和断言。
4. 旧记录缺 ``input_sources`` 时显式 ``(未标注)``, 不假装是系统算的。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "_decision_explain", ROOT / "src" / "aterag" / "inference" / "decision_explain.py"
)
assert _spec and _spec.loader
dx = importlib.util.module_from_spec(_spec)
sys.modules["_decision_explain"] = dx
_spec.loader.exec_module(dx)


@pytest.fixture()
def entry() -> dict:
    """板上实测过的真实形态 (dec:03b8ebe7 那条的结构)。"""
    return {
        "entity_id": "dec:03b8ebe7-c0a2-4354-8f25-52e705cbfbd9",
        "entity_type": "decision",
        "agent_id": "aterag.inference.engine",
        "source_document": "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=0082",
        "confidence": 0.95,
        "checksum": "678c8d9e367c326f",
        "previous_checksum": "888ab95559eb2431",
        "metadata": {
            "credibility": 0.95,
            "confidence": 0.95,
            "confidence_unknown": False,
            "rule_id": "K-ELEC-001",
            "statement": "直流功率 = 电压 × 电流 (P = U × I)",
            "output": "power",
            "value": "599.4",
            "domain_layer": "power",
            "inputs": {"voltage": "54.0", "current": "11.1"},
            "input_sources": {
                "voltage": {"req_id": "SR-PA601-D54A-1200", "typ": 54.0},
                "current": "(caller)",
            },
        },
    }


class TestStructuredExplanation:
    def test_premises_carry_provenance(self, entry: dict) -> None:
        exp, _ = dx.explain(entry)
        path = exp.reasoning_path
        premises = [s for s in path.steps if s.metadata.get("kind") == "premise"]
        # 两个输入都有出处, 调用方给的显式 (caller)
        assert premises[0].description == "voltage = 54.0 <- SR-PA601-D54A-1200"
        assert premises[1].description == "current = 11.1 <- (caller)"
        assert path.end_conclusion == "power = 599.4"

    def test_rule_step_points_at_rule(self, entry: dict) -> None:
        exp, _ = dx.explain(entry)
        last = exp.reasoning_path.steps[-1]
        assert last.rule_applied.rule_id == "K-ELEC-001"
        assert "GB/T" not in last.rule_applied.name  # statement 不是出处
        # 谱系行级别名可追: checksum 链在 metadata 里
        assert exp.reasoning_path.metadata["checksum"] == "678c8d9e367c326f"

    def test_deterministic(self, entry: dict) -> None:
        """解释要能被 diff/断言: 两次内容相同, id 不掺时间戳。"""
        a_exp, a_text = dx.explain(entry)
        b_exp, b_text = dx.explain(entry)
        assert a_exp.explanation_id == b_exp.explanation_id == "exp_dec:03b8ebe7-c0a2-4354-8f25-52e705cbfbd9"
        assert a_text == b_text


class TestNoTopGrading:
    def test_missing_credibility_not_ones(self, entry: dict) -> None:
        """未标注可信度 -> 0.0 + 标记 + 文本「未标注」, 无一处 1.0。"""
        entry["metadata"].pop("credibility")
        entry["confidence"] = None
        exp, text = dx.explain(entry)
        path = exp.reasoning_path
        assert path.total_confidence == 0.0
        assert path.steps[-1].confidence == 0.0
        assert path.metadata["credibility_unknown"] is True
        assert "未标注" in text
        # 1.0 只可能是上游缺省 —— 这里任何一个结构字段都不得出现
        assert "1.00" not in text

    def test_known_credibility_flows_through(self, entry: dict) -> None:
        exp, text = dx.explain(entry)
        assert exp.reasoning_path.total_confidence == 0.95
        assert "出处可信度 0.95" in text


class TestMissingFields:
    def test_legacy_record_without_input_sources(self) -> None:
        """旧记录没有 input_sources: 显式 (未标注), 不编 SR 号。"""
        exp, text = dx.explain(
            {
                "entity_id": "dec:old",
                "metadata": {
                    "rule_id": "K-ELEC-002",
                    "output": "loss",
                    "value": "5.7",
                    "inputs": {"pin": "605.1", "power": "599.4"},
                },
            }
        )
        premises = [s for s in exp.reasoning_path.steps if s.metadata.get("kind") == "premise"]
        assert premises[0].description == "pin = 605.1 <- (未标注)"
        assert "(未标注)" in text
        # 顶层 confidence 缺省时也不顶格
        assert exp.reasoning_path.total_confidence == 0.0

    def test_input_without_value_still_listed(self, entry: dict) -> None:
        """有出处没数值(旧链路只记了 input_sources)也要成行。"""
        entry["metadata"]["inputs"] = {}
        exp, _ = dx.explain(entry)
        premises = [s for s in exp.reasoning_path.steps if s.metadata.get("kind") == "premise"]
        assert premises[0].description == "voltage = (未提供) <- SR-PA601-D54A-1200"


def test_audit_text_reads_as_audit(entry: dict) -> None:
    _exp, text = dx.explain(entry)
    assert "决策 03b8ebe7-c0a2-4354-8f25-52e705cbfbd9" in text
    assert "规则 K-ELEC-001" in text
    assert "voltage = 54.0 <- SR-PA601-D54A-1200" in text
    assert "结论 power = 599.4" in text
    # 规则出处给的是可核的标准平台地址
    assert "openstd.samr.gov.cn" in text
