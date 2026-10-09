"""验收准则: 知识覆盖度门禁 (authority_coverage)。

为什么要有这组测试
------------------
2026-10 那一轮补数据把种子的 unverified 从 431 降到 28、孤立率从 74.0% 降到 48.1%,
但**当时没有任何东西盯着**: 门禁没有这一项, 于是改善不可见, **回退也不可见** ——
把实体倒回 unverified、把 confidence 删掉, 门禁照样全绿。

所以这组测试钉的是**规则 + 双向可测**:
  - 真实种子必须达标(否则检查形同虚设);
  - 人为退化必须被抓到(否则检查是摆设)。
只测「真实种子达标」是不够的 —— 那正是本次要修的漏洞本身。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import knowledge_gate as kg  # noqa: E402

SEED = ROOT / "data" / "seed" / "power_domain_seed.json"


@pytest.fixture(scope="module")
def seed_records() -> list[dict]:
    return json.loads(SEED.read_text(encoding="utf-8"))["records"]


def _ents(records: list[dict]) -> list[dict]:
    return [r for r in records if not r.get("source_id")]


class TestRealSeedMeetsTheBar:
    """真实种子必须达标 —— 否则这条检查对生产数据没有约束力。"""

    def test_no_coverage_findings(self, seed_records):
        rep = kg.run_gate(seed_records)
        bad = [f for f in rep.findings if f.check == "authority_coverage"]
        assert not bad, "真实种子不该触发覆盖度告警: %s" % [f.detail for f in bad]

    def test_ratios_recorded_in_stats(self, seed_records):
        rep = kg.run_gate(seed_records)
        assert rep.stats["unverified_ratio"] <= kg.UNVERIFIED_RATIO_MAX
        assert rep.stats["confidence_cover"] >= kg.CONFIDENCE_COVER_MIN
        assert rep.stats["relation_self_loops"] == 0

    def test_stats_expose_the_authority_split(self, seed_records):
        """权威分层必须进 stats —— 分层变了要看得见, 而不是只在报告里消失。"""
        rep = kg.run_gate(seed_records)
        ak = rep.stats["authority_by_kind"]
        assert set(ak) <= {"standard", "industry", "unverified", "project_defined", "spec", "book"}
        assert sum(ak.values()) == len(_ents(seed_records))


class TestRegressionIsCaught:
    """退化必须被抓到。**只测达标是不够的** —— 那正是本次要修的漏洞。"""

    def test_mass_downgrade_to_unverified_fires(self, seed_records):
        ents = _ents(seed_records)
        victims = {r["id"] for r in ents if kg._authority_kind(r) == "standard"}
        assert len(victims) > 50, "样本要够大才能越过阈值"
        mut = []
        for r in seed_records:
            if r.get("id") in victims:
                r2 = dict(r)
                r2["metadata"] = {**(r.get("metadata") or {}), "authority_kind": "unverified"}
                mut.append(r2)
            else:
                mut.append(r)
        rep = kg.run_gate(mut)
        assert rep.stats["unverified_ratio"] > kg.UNVERIFIED_RATIO_MAX
        assert any(
            f.check == "authority_coverage" and "unverified" in f.detail for f in rep.findings
        ), "把大批实体倒回未查证必须报出来"

    def test_stripping_confidence_fires(self, seed_records):
        mut = []
        for r in seed_records:
            if r.get("confidence") is not None and not r.get("source_id"):
                r2 = dict(r)
                r2.pop("confidence", None)
                mut.append(r2)
            else:
                mut.append(r)
        rep = kg.run_gate(mut)
        assert rep.stats["confidence_cover"] < kg.CONFIDENCE_COVER_MIN
        assert any(
            f.check == "authority_coverage" and "confidence" in f.detail for f in rep.findings
        )

    def test_self_loops_fire(self, seed_records):
        """自环必须在报告里可见 —— 它使「关系记录数」与「实际建边数」对不上。"""
        loop = {
            "source_id": "thm::T1",
            "target_id": "thm::T1",
            "relationship_type": "has_theorem",
            "properties": {},
        }
        rep = kg.run_gate(seed_records + [dict(loop) for _ in range(3)])
        assert rep.stats["relation_self_loops"] == 3
        assert any(f.check == "authority_coverage" and "自环" in f.detail for f in rep.findings)

    def test_real_seed_has_no_self_loops(self, seed_records):
        """关系记录里 source==target 的条数必须为 0。

        实测曾有 23 条(种子自身 20 条 + 本轮补标准时又产生 3 条)。它们不含信息,
        下游 materialize 建图时全部丢弃, 于是**关系记录数永远不等于建边数** ——
        按记录数审图的人会把它们当成真边。已在 build_relationships 里排除。
        """
        loops = [
            r
            for r in seed_records
            if r.get("source_id") and str(r.get("source_id")) == str(r.get("target_id"))
        ]
        assert not loops, "种子里有 %d 条自环: %s" % (
            len(loops),
            [r["source_id"] for r in loops[:5]],
        )

    def test_relation_records_equal_built_edges(self, seed_records):
        """关系记录数必须等于实际建出的边数。

        这两条数对不上时, 只有一个原因: 记录里有建不出来的边(自环/悬空端点)。
        少建是必然的(materialize 会过滤), 所以「记录 == 边」是唯一自洽的状态。
        """
        sys.path.insert(0, str(ROOT / "src"))
        from aterag.kg import analytics

        g = analytics.graph_from_records(seed_records)
        topo = analytics.topology(g)
        n_rel = sum(1 for r in seed_records if r.get("source_id"))
        assert n_rel == topo["edges"], (
            "关系记录 %d 条, 实际建边 %d 条 —— 差 %d 条说明有边建不出来"
            % (n_rel, topo["edges"], n_rel - topo["edges"])
        )


class TestCoverageIsNotBrittle:
    """阈值不能薄到「多一条就红」—— 那会让检查被当噪声忽略, 比没有更糟。"""

    def test_thresholds_leave_headroom(self):
        rep = kg.run_gate(json.loads(SEED.read_text(encoding="utf-8"))["records"])
        assert rep.stats["confidence_cover"] - kg.CONFIDENCE_COVER_MIN >= 0.03, (
            "confidence 覆盖率离阈值太近(%.4f vs %.2f), 再少一条就翻红"
            % (rep.stats["confidence_cover"], kg.CONFIDENCE_COVER_MIN)
        )
        assert kg.UNVERIFIED_RATIO_MAX - rep.stats["unverified_ratio"] >= 0.05, (
            "unverified 占比离阈值太近, 再多几条就翻红"
        )

    def test_threshold_breaches_are_warn_not_error(self, seed_records):
        """越过阈值判 WARN 不判 ERROR —— 永远红的门禁会被整体忽略。

        注意这里测的是**阈值告警**的严重度, 不是「检查里不出现 ERROR」:
        「记录里一条实体都没有」判 ERROR 是对的(那是数据没了, 不是工作没做完),
        一刀切禁掉 ERROR 会把这个真缺陷也放过。所以只验证阈值那三条是 WARN。
        """
        ents = _ents(seed_records)
        downgrade = {r["id"] for r in ents if kg._authority_kind(r) in ("standard", "industry")}
        mut = []
        for r in seed_records:
            if r.get("id") in downgrade:
                r2 = dict(r)
                r2["metadata"] = {**(r.get("metadata") or {}), "authority_kind": "unverified"}
                r2.pop("confidence", None)
                mut.append(r2)
            else:
                mut.append(r)
        mut = mut + [
            {
                "source_id": "thm::T1",
                "target_id": "thm::T1",
                "relationship_type": "has_theorem",
                "properties": {},
            }
        ]
        rep = kg.run_gate(mut)
        cov = [f for f in rep.findings if f.check == "authority_coverage"]
        assert len(cov) >= 3, "三种退化都该被报出来: %s" % [f.detail for f in cov]
        assert all(f.severity == "WARN" for f in cov), "阈值告警不该判 ERROR: %s" % [
            (f.severity, f.detail) for f in cov
        ]

    def test_empty_seed_is_still_an_error(self):
        """空种子判 ERROR —— 「数据没了」和「工作没做完」是两回事, 不能一起降级。"""
        rep = kg.run_gate([])
        cov = [f for f in rep.findings if f.check == "authority_coverage"]
        assert cov and cov[0].severity == "ERROR"
