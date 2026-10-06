"""冲突消解红线钉住测试: 只用 credibility, fail-open 一律关闭。

四条上游 fail-open(0.7.0 实测, 见 adapter 模块 docstring)全部在这里
有对应用例钉死 —— 这些不是「上游可以改好」的问题, 而是本桥出口就不
可能放进去的问题。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "_aterag_conflicts", ROOT / "src" / "aterag" / "conflicts" / "__init__.py"
)
assert _spec and _spec.loader
conf = importlib.util.module_from_spec(_spec)
sys.modules["_aterag_conflicts"] = conf
_spec.loader.exec_module(conf)


def _records(standard_value: float = 54.0, other_value: float = 51.0):
    """两来源一值打架: 国标(0.9) vs 未核出处(0.2)。

    confidence: 国标标注 0.95(出处可核), 未核的标 1.0 —— 它自己
    「不知道自己可不可信」, 所以不敢标低, 这正是门要防的写法。
    """
    return [
        {
            "value": standard_value,
            "source_document": "GB/T 2900.1-2008",
            "confidence": 0.95,
            "authority_kind": "standard",
        },
        {
            "value": other_value,
            "source_document": "内部笔记-未查证",
            "confidence": 1.0,
            "authority_kind": "unverified",
        },
    ]


class TestStrategyRedline:
    @pytest.mark.parametrize(
        "strategy",
        ["voting", "most_recent", "first_seen", "highest_confidence"],
    )
    def test_forbidden_strategy_unreachable(self, strategy: str) -> None:
        """投票/时间/到达序/自标 conf — 四类排序依据在桥口就抛。

        上游给了 7 个策略枚举值, 红线只放行 credibility_weighted;
        manual_review/expert_review 由桥自己出(非消解出口), 也不透传。
        """
        r = conf.CredibilityOnlyConflictResolver()
        with pytest.raises(ValueError, match="credibility"):
            r.resolve(
                conf.make_conflict("E1", "value", [54.0, 51.0], []),
                strategy=strategy,
            )

    def test_default_is_credibility_not_voting(self) -> None:
        """上游缺省是 'voting' —— 本桥构造时显式改掉, 靠「每个调用点
        记得传参」防不住((实测上游 ``config.get('default_strategy',
        'voting')``)。"""
        r = conf.CredibilityOnlyConflictResolver()
        assert r._resolver.default_strategy == conf.ONLY_STRATEGY


class TestFailClosed:
    def test_unregistered_source_goes_to_review(self) -> None:
        """未登记文档: 上游会给 0.5 参与加权 —— 本桥转 review。

        0.5 能输给权威但**赢过** 0.2 级出处, 属于「沉默地让半可信的东西
        有话语权」; 分值必须显式登记, 没登记就人审。
        """
        r = conf.CredibilityOnlyConflictResolver()
        got = r.resolve(
            conf.make_conflict(
                "E1",
                "value",
                [54.0, 51.0],
                [
                    {"document": "GB/T 2900.1-2008", "confidence": 0.95},
                    {"document": "没有登记过的文档", "confidence": 0.9},
                ],
            )
        )
        assert not got.resolved
        assert got.resolved_value is None
        assert "未登记" in got.resolution_notes
        assert got.sources_used == ["GB/T 2900.1-2008", "没有登记过的文档"]

    def test_upstream_zero_point_five_documented(self) -> None:
        """**钉住上游行为**供回归观察: SourceTracker 未登记 -> 0.5。

        这条不是本桥的行为(本桥不进这条路径), 是记录我们防御的对象,
        上游若改掉 fail-open, 这条测试提醒更新 adapter 注释。
        """
        from semantica.conflicts.conflict_detector import SourceTracker

        assert SourceTracker().get_source_credibility("不在册") == 0.5

    def test_missing_source_confidence_goes_to_review(self) -> None:
        """来源记录没标 confidence: 上游 0.5 缺省参与加权 —— 桥拒收。"""
        recs = _records()
        for rec in recs:
            rec.pop("confidence")
        r = conf.CredibilityOnlyConflictResolver()
        got = conf.adjudicate("K-PWR-1", "threshold", recs, r)
        assert got.outcome == "review"
        assert got.value is None
        assert "confidence" in got.notes

    def test_unknown_authority_kind_raises(self) -> None:
        """分级表外的 authority_kind 抛错, 不猜分值。

        真的表外例子: 厂商网页/百科类来源(`vendor_web`)。这类来源不是
        没有可信度, 而是**没定过档** —— 定档是跨型号跨产品的判断, 不能由
        某个调用点随手给。报错的去处是补 `CREDIBILITY_BY_AUTHORITY`
        (一处改, 全库同量纲), 不是在本层铸个私下分值。(`spec` 曾是
        表外 kind, 已定档 0.9, 见 TestTieGoesToReview。)
        """
        r = conf.CredibilityOnlyConflictResolver()
        with pytest.raises(conf.UnknownAuthorityKind, match="CREDIBILITY_BY_AUTHORITY"):
            r.register_source("vendor_web:x", authority_kind="vendor_web")


class TestResolution:
    def test_credibility_wins_over_confidence(self) -> None:
        """**credibility 赢, 不是 confidence 赢**: 未核出处的自标 1.0
        打不过国标分档加权(0.95*0.9=0.855 > 1.0*0.2=0.2)。"""
        got = conf.adjudicate("K-PWR-1", "v", _records())
        assert got.outcome == "resolved"
        assert got.value == 54.0
        # 输家保留在记录里, 复查能回答「另一个值为什么输了」
        assert len(got.sources) == 2
        assert "credibility" in got.notes.lower() or resolved_note(got.notes)

    def test_single_source_accepted(self) -> None:
        got = conf.adjudicate("K-PWR-1", "v", _records()[:1])
        assert got.outcome == "single"
        assert got.value == 54.0

    def test_unanimous_multi_source_accepted(self) -> None:
        recs = _records()
        recs[1]["value"] = 54.0  # 两个来源同值 = 互证, 不是冲突
        got = conf.adjudicate("K-PWR-1", "v", recs)
        assert got.outcome == "unanimous"
        assert got.value == 54.0

    def test_float_noise_is_not_conflict(self) -> None:
        """599.4 vs 599.4000001 是同一物理量, ``==`` 会把互证误判成冲突
        (嵌入那边实测 cos != 1.0 的同款教训: 数值比较莫用严格等)。"""
        recs = _records(599.4, 599.4000001)
        got = conf.adjudicate("K-PWR-1", "p_out", recs)
        assert got.outcome == "unanimous"


class TestTieGoesToReview:
    def test_equal_credibility_is_a_tie_not_a_winner(self) -> None:
        """**平局不是胜者**。国标 0.9 与厂商规格书 0.9(刚定为同级),
        各自 confidence 也相同 -> 权重严格相等。

        上游此时取 ``max()`` 的**先遇到**项, 即输入顺序决定胜负; 换个
        顺序就换个赢家。这里断言: 倒序重解会换人 -> 转人审, 不猜。
        """
        recs = [
            {
                "value": 54.0,
                "source_document": "GB/T 2900.1-2008",
                "confidence": 0.95,
                "authority_kind": "standard",
            },
            {
                "value": 51.0,
                "source_document": "spec:PA601-D54A",
                "confidence": 0.95,
                "authority_kind": "spec",
            },
        ]
        got = conf.adjudicate("K-PWR-1", "v", recs)
        assert got.outcome == "review", got.notes
        assert got.value is None
        assert "平局" in got.notes

    def test_upstream_tie_is_order_dependent_documented(self) -> None:
        """钉住上游现状: 权重相等时上游**确实**按输入顺序给答案。

        与上一条配对: 那条证明本桥会拦住它, 这条证明它真是上游行为而不
        是本桥的错觉。上游若改成稳定的择一, 这条会提醒更新注释。
        """
        from semantica.conflicts.conflict_detector import SourceTracker
        from semantica.conflicts.conflict_resolver import ConflictResolver

        tracker = SourceTracker()
        for doc in ("A", "B"):
            tracker.register_source(doc, "standard", credibility_score=0.9)
        resolver = ConflictResolver(default_strategy="credibility_weighted")
        resolver.set_source_tracker(tracker)
        c = conf.make_conflict(
            "E",
            "v",
            [54.0, 51.0],
            [
                {"document": "A", "confidence": 0.95},
                {"document": "B", "confidence": 0.95},
            ],
        )
        fwd = resolver.resolve_conflict(c)
        bwd = resolver.resolve_conflict(
            conf.make_conflict(
                "E",
                "v",
                [51.0, 54.0],
                [
                    {"document": "B", "confidence": 0.95},
                    {"document": "A", "confidence": 0.95},
                ],
            )
        )
        assert str(fwd.resolved_value) != str(bwd.resolved_value)

    def test_spec_grade_is_registerable(self) -> None:
        """厂商规格书有了档位(与国标同级), 能进裁决门。

        之前的悬案: model 记录用 ``authority_kind='spec'``, 不在分级表
        里, 门直接拒收。现在定档 0.9 —— 不降级(型号特定需求只它说话),
        不升级(通用物理量的权威是国标)。
        """
        r = conf.CredibilityOnlyConflictResolver()
        assert r.register_source("spec:PA601-D54A", authority_kind="spec") == 0.9
        assert r.credibility_of("spec:PA601-D54A") == 0.9


def resolved_note(notes: str) -> bool:
    return "credibility" in notes or "加权" in notes or "weight" in notes


__all__ = []
