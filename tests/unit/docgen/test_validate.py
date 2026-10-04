"""``docgen.validate`` 的单元测试 —— §18.9 门禁 G1~G10。

重点测两件事:

1. **门禁不能空转。** 分母取「已入库集」时 G1 恒为 100%, 那是个假 PASS;
   未实现的门禁若报 PASS 就是假绿灯。这两类都必须被测试挡住。
2. **结论必须带可核查的 evidence**, 而不是结论性描述 —— 门禁的价值就在于
   人能顺着明细复核。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aterag.docgen.validate import (
    SPEC_PATH,
    Corpus,
    Level,
    Status,
    load_corpus,
    main,
    run_gates,
)

SPEC = Path(SPEC_PATH)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    if not SPEC.is_file():
        pytest.skip(f"方案文件不在预期位置: {SPEC}")
    return load_corpus(SPEC)


@pytest.mark.slow
class TestCorpus:
    def test_counts(self, corpus: Corpus) -> None:
        assert len(corpus.first) == 545
        assert len(corpus.parseable) == 369
        assert len(corpus.closed) == 129

    def test_derivable_excludes_deliberately_unruled(self, corpus: Corpus) -> None:
        """「可推导」不该把被**方案歧义**挡住的公式算进来。

        卡在 ``s``/``T``/``k`` 上的公式是方案自身没给出足够信息, 不是我方
        覆盖缺口。算进分母会让覆盖率被压低, 于是补符号表看起来永远补不满。
        """
        derivable_ids = set(corpus.derivable)
        blocked = set(corpus.parseable) - set(corpus.closed)
        # 可推导集是「可解析未闭合」的子集, 且必然更小
        assert derivable_ids < blocked


@pytest.mark.slow
class TestGateHonesty:
    """门禁不能给出误导性结论。"""

    def test_g1_is_not_a_vacuous_pass(self, corpus: Corpus) -> None:
        """G1 **不得**报 PASS。

        若分母取已入库集(只含闭合公式), 比值恒为 100%。那不是「量纲已校验」,
        而是没有数据可校验。所以本阶段必须是 NOT_APPLICABLE。
        """
        (g1,) = [r for r in run_gates(["G1"], SPEC) if r.gate_id == "G1"]
        assert g1.status is Status.NOT_APPLICABLE
        assert not g1.blocks_merge

    def test_g1_reports_all_three_denominators(self, corpus: Corpus) -> None:
        """覆盖率必须并列给出三个分母, 不能只挑一个好看的。"""
        (g1,) = [r for r in run_gates(["G1"], SPEC) if r.gate_id == "G1"]
        for marker in ("分母全部545", "分母可解析", "分母可推导"):
            assert marker in g1.detail, f"缺分母 {marker}: {g1.detail}"

    @pytest.mark.parametrize("gid", ["G2", "G3", "G8", "G9"])
    def test_unimplemented_gates_are_never_pass(self, gid: str) -> None:
        """依赖 W2/W3/W6 的门禁必须报 N/A —— 假绿灯比红灯危险。"""
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        (result,) = [r for r in run_gates([gid], SPEC) if r.gate_id == gid]
        assert result.status is Status.NOT_APPLICABLE
        assert result.status is not Status.PASS
        assert not result.blocks_merge
        assert "依赖未就绪" in result.detail

    def test_every_gate_has_evidence(self, corpus: Corpus) -> None:
        """每条门禁都要给出可核查的明细。"""
        for result in run_gates(None, SPEC):
            assert result.evidence, f"{result.gate_id} 没有任何 evidence"
            assert result.detail, f"{result.gate_id} 没有任何 detail"

    def test_g10_admits_unchecked_subchecks(self, corpus: Corpus) -> None:
        """G10 有两项子判据没跑, 必须**明说**, 不许看起来像全查过了。"""
        (g10,) = [r for r in run_gates(["G10"], SPEC) if r.gate_id == "G10"]
        assert "子判据未跑" in g10.detail
        assert any("未跑" in e for e in g10.evidence)


@pytest.mark.slow
class TestGateResults:
    def test_g5_errata_all_linked(self, corpus: Corpus) -> None:
        """G5: 7 条勘误全部关联到实际公式(判据 3)。"""
        (g5,) = [r for r in run_gates(["G5"], SPEC) if r.gate_id == "G5"]
        assert g5.status is Status.PASS
        assert "7/7" in g5.detail

    def test_g5_keeps_provenance_labels(self, corpus: Corpus) -> None:
        """勘误来源类别(正文点名 / 声明)必须出现在 evidence 里。

        审计时要能分清哪些关联有文档支撑、哪些是我方补的。
        """
        (g5,) = [r for r in run_gates(["G5"], SPEC) if r.gate_id == "G5"]
        blob = " ".join(g5.evidence)
        assert "正文点名" in blob
        assert "声明" in blob

    def test_g6_flags_undeterminable_direction(self, corpus: Corpus) -> None:
        """G6 正向不可判定时必须明说, 不拿反向结果冒充正向通过。"""
        (g6,) = [r for r in run_gates(["G6"], SPEC) if r.gate_id == "G6"]
        assert "正向不可判定" in g6.detail
        assert g6.status is not Status.PASS

    def test_g4_reports_missing_derive_from(self, corpus: Corpus) -> None:
        """G4 缺 derive_from 是真实缺口, 必须 FAIL 而不是放水。"""
        (g4,) = [r for r in run_gates(["G4"], SPEC) if r.gate_id == "G4"]
        assert g4.status is Status.FAIL
        assert g4.level is Level.MERGE_BLOCK
        assert g4.blocks_merge
        assert g4.evidence

    def test_unknown_gate_rejected(self) -> None:
        with pytest.raises(SystemExit):
            run_gates(["G99"], SPEC)


class TestCli:
    def test_exit_code_reflects_merge_block(self) -> None:
        """有合并阻断项失败时退出码非 0。"""
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        assert main(["--all", "--spec", str(SPEC)]) == 1

    def test_exit_zero_when_subset_passes(self) -> None:
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        assert main(["--gates", "G5", "--spec", str(SPEC)]) == 0

    def test_requires_a_selector(self) -> None:
        with pytest.raises(SystemExit):
            main([])
