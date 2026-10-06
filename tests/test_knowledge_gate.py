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

    def test_top_graded_confidence_is_warning(self) -> None:
        """顶格 1.0 不阻断但必须被点名。

        本项目因顶格栽过三次: 上游 ``track_entity`` 缺省 1.0、
        ``ReasoningStep`` 缺省 1.0、知识侧「项目约定 = 1.0」把约定显示成
        已验证的外部事实。所以顶格要显式、要有人看见。
        """
        rep = kg.run_gate([_rec(confidence=1.0)])
        assert _errors(rep) == []
        assert any(f.check == "confidence_top_graded" for f in _warns(rep))

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

    def test_compound_binding_is_split_before_matching(self) -> None:
        """``bindings`` 一个值里塞了好几条(``"F_L.2.5、G.39"``)。

        不拆就会把整串当一个值比, ``"F_L.2.5、G.39"`` 天然不等于任何 id ——
        于是每条这样的值都报一次「解析不到」, 而真问题一个都看不见。
        """
        recs = [
            _rec(id="F_L.2.5_RAILWAY_FUNCTIONAL_SAFETY", entity_type="formula"),
            _rec(authority_kind="standard", authority_ref="GB/T 1-2020",
                 bindings="F_L.2.5、G.39"),
        ]
        rep = kg.run_gate(recs)
        assert [f for f in _warns(rep) if f.check == "undeclared_ref"] == []

    def test_short_ref_resolves_by_unique_prefix(self) -> None:
        """短记号(``F_L.2.7``)按前缀找到**唯一**全名就算解析得到。

        方案 md 引用公式只写章节号, 本库 id 带名字后缀, 是记法差异。
        """
        recs = [
            _rec(id="F_L.2.7_DEADBAND_MIN", entity_type="formula"),
            _rec(authority_kind="standard", authority_ref="GB/T 1-2020", bindings="F_L.2.7"),
        ]
        assert not [f for f in _warns(kg.run_gate(recs)) if f.check == "undeclared_ref"]

    def test_ambiguous_short_ref_is_still_reported(self) -> None:
        """前缀命中多条 = 记号有歧义, **不许蒙一条**。"""
        recs = [
            _rec(id="F_L.2.7_DEADBAND_MIN", entity_type="formula"),
            _rec(id="F_L.2.7_DEADBAND_MAX", entity_type="formula"),
            _rec(authority_kind="standard", authority_ref="GB/T 1-2020", bindings="F_L.2.7"),
        ]
        warns = [f for f in _warns(kg.run_gate(recs)) if f.check == "undeclared_ref"]
        assert warns and "F_L.2.7" in warns[0].detail

    def test_unresolved_tokens_are_listed_not_just_counted(self) -> None:
        """报**不同的记号**: 26 次里有一半是同一个记号被多条标准引用,
        按次数看是 26 个问题, 按记号看是 8 个。"""
        recs = [
            _rec(authority_kind="standard", authority_ref="GB/T 1-2020", bindings="F_G.9"),
            _rec(authority_kind="standard", authority_ref="GB/T 2-2020", bindings="F_G.9"),
            _rec(authority_kind="standard", authority_ref="GB/T 3-2020", bindings="F_H.9"),
        ]
        warns = [f for f in _warns(kg.run_gate(recs)) if f.check == "undeclared_ref"]
        assert len(warns) == 1
        assert "2 种" in warns[0].detail, warns[0].detail


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

    def test_ids_all_match_namespace_or_are_registered_bare_names(self) -> None:
        """``ID_NAMESPACE`` 是通用扫描的判据 —— 不匹配它的 id 只能是**已登记的
        裸名形态**, 出现第三类就得有人重新审视判据。

        勘误号曾经也在这张单子上(``err::`` 是后加的): ``E-1`` 裸号与公式的
        章节记号形态完全撞车, 所以勘误加了前缀, 从裸名名单里移出去了。
        剩下的裸名只有 ``power_concept``(概念名 / 遥信遥测遥代码 / SR 字段名
        直接当 id)。
        """
        recs = self._records()
        unmatched = {
            str(r["id"]) for r in recs
            if r.get("id") and not kg.ID_NAMESPACE.match(str(r["id"]))
        }
        assert unmatched, "预期存在已登记的裸名 id; 若已全部规范化, 请更新门禁判据"
        types = {
            str(r["entity_type"])
            for r in recs
            if r.get("id") and str(r["id"]) in unmatched
        }
        assert types == kg.BARE_NAME_ID_TYPES, types
        assert kg.BARE_NAME_ID_TYPES == frozenset({"power_concept"}), (
            "裸名 id 名单变了: 新增类型要说明它为什么不能进 ID_NAMESPACE, "
            "移出类型要说明它的 id 前缀是什么"
        )

    def test_id_namespace_warn_reports_the_whole_number(self) -> None:
        """失配数**不许静默截断**。

        第一版是 ``stray[:10]``, 真种子上有 161 条失配却只报 10 条 —— 少报
        151 条已登记形态和少报真问题在这里是同一种静默。
        """
        recs = [_rec(id=f"BAD{i}", entity_type="axiom") for i in range(12)]
        rep = kg.run_gate(recs)
        warns = [f for f in _warns(rep) if f.check == "id_namespace"]
        assert len(warns) == 1
        assert "12 条" in warns[0].detail, warns[0].detail

    def test_bare_name_ids_are_counted_in_stats(self) -> None:
        """已登记的裸名 id 不点名, 但**要计数** —— 形态漂移要看得见。"""
        rep = kg.run_gate([_rec(id="BMS", entity_type="power_concept"),
                           _rec(id="BADFORM", entity_type="axiom")])
        # 一条登记过的裸名 + 一条没登记的: 只有后者该被点名
        assert rep.stats["bare_name_ids"] == 1
        warns = [f for f in _warns(rep) if f.check == "id_namespace"]
        assert len(warns) == 1 and "1 条" in warns[0].detail
        assert "BADFORM" in warns[0].detail and "BMS" not in warns[0].detail

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

    def test_load_conventions_have_locatable_authority(self) -> None:
        """工况比例是**项目约定**, 出处落在 corrections.yaml 的约定记录段。

        之前 11 条里 9 条 authority_ref 为空(在知识门里算「无出处」), 另
        两条把 authority_kind 标成 ``industry`` 且 confidence 顶格 1.0 ——
        会让下游以为有行业标准背书。断言的是「类别与出处落点」, 不是具体
        字串, 免得约定记录挪位置就红。
        """
        recs = self._records()
        loads = [
            r
            for r in recs
            if r.get("entity_type") in ("load_condition", "load_ratio") and r.get("id")
        ]
        assert len(loads) == 11
        for r in loads:
            kind = (r.get("metadata") or {}).get("authority_kind")
            assert kind == "project_defined", (r["id"], kind)
            assert r.get("authority_ref"), r["id"]
            # 顶格已被纠正为约定档
            assert r.get("confidence") in (None, 0.5), (r["id"], r.get("confidence"))

    def test_missing_confidence_is_split_by_whether_kind_is_declared(self) -> None:
        """「没标 confidence」要分成两种报, 因为下一步动作不同。

        声明了 ``authority_kind`` 的, 可信度已经能从
        ``CREDIBILITY_BY_AUTHORITY`` 推出, 只差这次断言本身查没查过; 连权威
        类型都没有的, 是连「该拿哪份标准去查」都还不知道。只报总数的话,
        「588 条」这个数字驱动不了任何补齐工作。
        """
        rep = kg.run_gate([
            _rec(id="a", authority_kind="unverified"),
            _rec(id="b"),
            _rec(id="c", confidence=0.5),
        ])
        detail = [f.detail for f in _warns(rep) if f.check == "confidence_present"][0]
        assert "2 条未标 confidence" in detail, detail
        assert "1 条已声明 authority_kind" in detail, detail

    def test_gate_cli_exit_zero_on_real_seed(self) -> None:
        assert kg.main(["--seed", str(SEED)]) == 0
