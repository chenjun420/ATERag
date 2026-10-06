"""知识门行为钉定。

门的三条纪律各有用例:
1. **确定性判据的才 ERROR**(引用解析不到、id 重复、形态错、数值越界)
2. **合法状态不 ERROR**(无出处只是缺可信度)、**依赖前提的只 INFO**(孤儿)
3. **误报比漏报更危险** —— 第一版通用扫描一次报 305 条(绝大多数是
   ``text`` 内嵌自身 id), 所以未登记字段一律 WARN 汇总, 且真种子必须
   ERROR 归零
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "seed" / "power_domain_seed.json"

_spec = importlib.util.spec_from_file_location(
    "_knowledge_gate", ROOT / "scripts" / "knowledge_gate.py"
)
assert _spec and _spec.loader
kg = importlib.util.module_from_spec(_spec)
sys.modules["_knowledge_gate"] = kg
_spec.loader.exec_module(kg)


def _errors(report: Any) -> list[Any]:
    return [f for f in report.findings if f.severity == "ERROR"]


def _warns(report: Any) -> list[Any]:
    return [f for f in report.findings if f.severity == "WARN"]


def _rec(**kw: Any) -> dict[str, Any]:
    base = {"id": "x", "entity_type": "power_concept"}
    base.update(kw)
    return base


class TestBlockingChecks:
    def test_duplicate_id_is_error(self) -> None:
        rep = kg.run_gate([_rec(id="A"), _rec(id="A")])
        assert any(f.check == "id_unique" for f in _errors(rep))

    def test_dangling_formula_ref_is_error(self) -> None:
        rep = kg.run_gate([_rec(formula_refs=["F_E.1_OHM_LAW"])])
        assert any(f.check == "ref_resolves" for f in _errors(rep))

    def test_dangling_relation_endpoint_is_error(self) -> None:
        rep = kg.run_gate([{"source_id": "A", "target_id": "GONE", "relationship_type": "has"}])
        assert any(f.check == "ref_resolves" for f in _errors(rep))

    def test_standard_kind_with_clause_as_authority_is_error(self) -> None:
        """回归钉: 历史上 31 条遥信把方案章节号(``2.1.3``)当权威出处。

        形态检查的存在理由就是它 —— 「有出处」不等于「出处可核」。
        """
        rep = kg.run_gate(
            [_rec(authority_kind="standard", authority_ref="2.1.3", clause="2.1.3")]
        )
        errs = _errors(rep)
        assert any(f.check == "authority_shape" for f in errs)

    def test_standard_kind_without_ref_is_error(self) -> None:
        rep = kg.run_gate([_rec(authority_kind="standard")])
        assert any(f.check == "authority_shape" for f in _errors(rep))

    def test_confidence_out_of_range_is_error(self) -> None:
        assert any(
            f.check == "confidence_range"
            for f in _errors(kg.run_gate([_rec(confidence=1.5)]))
        )

    def test_confidence_non_numeric_is_error(self) -> None:
        """``True`` 是 bool 不是数值 —— 显式排除(bool 是 int 的子类)。"""
        rep = kg.run_gate([_rec(confidence=True)])
        assert any(f.check == "confidence_range" for f in _errors(rep))


class TestSeverityCalibration:
    def test_no_authority_is_warning_not_error(self) -> None:
        """无出处是知识层的合法状态(只降可信度), 报 ERROR 会逼着人编出处。"""
        rep = kg.run_gate([_rec()])
        assert _errors(rep) == []
        assert any(f.check == "authority_present" for f in _warns(rep))

    def test_orphan_is_info_only(self) -> None:
        rep = kg.run_gate([_rec(id="LONELY")])
        assert _errors(rep) == []
        assert any(f.severity == "INFO" and f.check == "orphan_records" for f in rep.findings)

    def test_clause_shape_is_warning(self) -> None:
        rep = kg.run_gate(
            [_rec(authority_kind="standard", authority_ref="GB/T 1-2020", clause="GB/T 1-2020")]
        )
        assert _errors(rep) == []
        assert any(f.check == "clause_shape" for f in _warns(rep))


class TestNoFalsePositives:
    def test_text_embedding_own_id_is_not_a_finding(self) -> None:
        """公式 ``text`` 的格式就是 ``\"<id>: <式子>`` —— 内嵌自身 id 是格式,
        不是引用。第一版把它当引用, 一次性报了 305 条 ERROR。"""
        rec = _rec(id="F_E.1_OHM_LAW", entity_type="formula", text="F_E.1_OHM_LAW: V = I*R")
        rep = kg.run_gate([rec])
        assert _errors(rep) == []

    def test_bindings_clause_tokens_are_warning(self) -> None:
        """``bindings`` 装的是条款记号(``R10、G.12``), 不是本库 id。"""
        rep = kg.run_gate(
            [_rec(authority_kind="standard", authority_ref="GB/T 1-2020", bindings="F_L.2.5、G.12")]
        )
        assert _errors(rep) == []
        assert any(f.check == "undeclared_ref" and f.where == "bindings" for f in _warns(rep))


class TestExitCode:
    def test_error_blocks(self) -> None:
        assert kg.run_gate([_rec(formula_refs=["NOPE"])]).exit_code() == 1

    def test_warning_does_not_block(self) -> None:
        assert kg.run_gate([_rec()]).exit_code() == 0


class TestRealSeed:
    """真种子上门禁必须 ERROR 归零。"""

    @staticmethod
    def _records() -> list[dict[str, Any]]:
        return list(json.loads(SEED.read_text(encoding="utf-8"))["records"])

    def test_no_errors_on_real_seed(self) -> None:
        rep = kg.run_gate(self._records())
        assert _errors(rep) == [], [f.to_dict() for f in _errors(rep)][:5]

    def test_ids_all_match_namespace_or_are_known_legacy(self) -> None:
        """``ID_NAMESPACE`` 是通用扫描的判据 —— 如果大量 id 不匹配它, 判据
        变弱, 至少要有人知道。``power_concept`` 的裸名 id 是历史形态。"""
        recs = self._records()
        ids = [str(r["id"]) for r in recs if r.get("id")]
        unmatched = [i for i in ids if not kg.ID_NAMESPACE.match(i)]
        assert unmatched, "预期存在历史形态的裸名 id; 若已全部规范化, 请更新门禁判据"
        # 裸名 id 是 **power_concept 与 erratum 两类**的历史形态(概念名
        # 直接当 id / 勘误号)。这两个类型是「已知的非命名空间类型」,
        # 新增第三类不匹配时要有人重新审视判据 —— 所以断言列全了。
        types = {
            str(r["entity_type"])
            for r in recs
            if r.get("id") and str(r["id"]) in set(unmatched)
        }
        assert types == {"power_concept", "erratum"}, types

    def test_undeclared_reference_fields_are_known(self) -> None:
        """安全网: **新字段**里出现 id 记号会红 —— 逼一次显式决定。

        当前已知的三个未登记字段是方案 md 带来的条款/上游记号
        (``bindings`` / ``upstream`` / ``scope``)。它们不是本库 id 引用,
        但也还没被登记成「非引用」—— 这个断言就是登记它们的地方。
        """
        recs = self._records()
        ids = {str(r["id"]) for r in recs if r.get("id")}
        found = set(kg.discover_reference_fields(recs, ids))
        assert found == {"bindings", "upstream", "scope"}, found

    def test_gate_cli_exit_zero_on_real_seed(self) -> None:
        assert kg.main(["--seed", str(SEED)]) == 0
