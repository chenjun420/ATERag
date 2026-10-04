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
        # ID 数 545 -> 573: 附录 U 的登记表没有表达式列, 早先被整行丢弃,
        # 现改为按字段合并 + 一格多 ID 展开。可解析/闭合数不变 ——
        # 合并不碰表达式。
        assert len(corpus.first) == 573
        assert len(corpus.parseable) == 369
        assert len(corpus.closed) == 130

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
        """G1 跑**真的**齐次性检查, 且有候选不齐次时必须 FAIL。

        早先两版都不可接受:

        - v1 取「已入库集」当分母, 比值恒 100% —— 空转的 PASS;
        - v2 报 ``NOT_APPLICABLE`` 说「表还没生成」—— 回避。

        现在接上 :func:`solver.symbolic.check_expression`, 实测 130 条候选里
        只有 113 条齐次, 所以 G1 **必须**红。把它改成 PASS 才是在骗人。
        """
        (g1,) = [r for r in run_gates(["G1"], SPEC) if r.gate_id == "G1"]
        assert g1.status is Status.FAIL
        assert g1.blocks_merge
        assert "不齐次" in g1.detail
        assert "100.0%" not in g1.detail, "比率不可能是 100%"

    def test_g1_reports_all_three_denominators(self, corpus: Corpus) -> None:
        """覆盖率必须并列给出三个分母, 不能只挑一个好看的。"""
        (g1,) = [r for r in run_gates(["G1"], SPEC) if r.gate_id == "G1"]
        for marker in ("分母全部573", "分母可解析", "分母可推导"):
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

    def test_g4_passes_but_names_the_pending_gap(self, corpus: Corpus) -> None:
        """D2 后 G4 判 PASS, 但**缺口必须具名**, 不能只报一个计数。

        「实验定律(待复核)」是 §18.9 G4 允许的标注形态, 故满足判据; 但这批
        公式并未真被核实为实验定律(例如 ``F_J.8.1_SHOOT_THROUGH`` 是电路分析),
        所以必须在 detail 与 evidence 里**逐条点名**, 否则一个绿灯就把它盖住了。
        """
        (g4,) = [r for r in run_gates(["G4"], SPEC) if r.gate_id == "G4"]
        assert g4.level is Level.MERGE_BLOCK
        assert g4.status is Status.PASS
        assert not g4.blocks_merge
        assert "待复核" in g4.detail
        blob = " ".join(g4.evidence)
        assert "待复核" in blob
        # 缺口要能点名: evidence 里应有具体 formula_id, 不只是一句话。
        assert any(fid.startswith("F_") for fid in g4.evidence)

    def test_g4_would_fail_if_a_formula_had_no_derivation(self) -> None:
        """安全网: 一旦某公式连标注都没有, G4 必须 FAIL。

        直接构造「未覆盖」的情形, 而不是依赖语料状态 —— 否则语料每改善一次
        这个测试就失去意义。
        """
        from aterag.docgen.spec_parse import FormulaRow
        from aterag.docgen.validate import Corpus, gate_g4

        corpus = Corpus(
            lines=[],
            first={"F_J.2.4_CUK": FormulaRow(formula_id="F_J.2.4_CUK")},
            closed={"F_J.2.4_CUK": ("D", "V_in")},
            derivations={},  # 没有任何补全结果 -> 未覆盖
        )
        assert gate_g4(corpus).status is Status.FAIL

    def test_unknown_gate_rejected(self) -> None:
        with pytest.raises(SystemExit):
            run_gates(["G99"], SPEC)


class TestCli:
    def test_exit_code_reflects_real_corpus_state(self) -> None:
        """真实语料下 ``--all`` 退出码非 0 —— 因为 G1 真红(7 条不齐次)。

        早先这里断言 0, 那时 G1 是空转 PASS。把断言对齐真实状态, 是为了让
        「红灯不会被悄悄改成绿灯」这件事在测试里留痕。
        """
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        assert main(["--all", "--spec", str(SPEC)]) == 1

    def test_exit_code_nonzero_on_merge_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """有合并阻断项失败时退出码非 0。

        用**合成**的门禁结果而不是依赖语料 —— 语料每改善一次(本次 D2 就把
        G4 从 FAIL 修成 PASS),依赖语料的断言就会失去意义。
        """
        from aterag.docgen import validate as V

        def fake(_ids: object = None, _spec: object = None) -> list[V.GateResult]:
            return [V.GateResult("G4", "公理可追溯", V.Level.MERGE_BLOCK, V.Status.FAIL, "合成")]

        monkeypatch.setattr(V, "run_gates", fake)
        assert V.main(["--all", "--spec", str(SPEC)]) == 1

    def test_exit_code_ignores_warning_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """警告级不阻断合并 —— G6/G7 是警告, 红了也不该卡住流水线。"""
        from aterag.docgen import validate as V

        def fake(_ids: object = None, _spec: object = None) -> list[V.GateResult]:
            return [V.GateResult("G7", "命名空间隔离", V.Level.WARNING, V.Status.FAIL, "合成")]

        monkeypatch.setattr(V, "run_gates", fake)
        assert V.main(["--all", "--spec", str(SPEC)]) == 0

    def test_exit_zero_when_subset_passes(self) -> None:
        if not SPEC.is_file():
            pytest.skip(f"方案文件不在预期位置: {SPEC}")
        assert main(["--gates", "G5", "--spec", str(SPEC)]) == 0

    def test_requires_a_selector(self) -> None:
        with pytest.raises(SystemExit):
            main([])
