"""LLM 起草通道 (方案 §4.2) 的不变量测试。

红线 (方案原文「红线」小节):
- LLM 只产出**草案**, 进 ``status: draft``, 运行时生效但标 ``proposed``
- 提案里每个子句的 ``text`` 必须能在规格书原文检索到, ``value`` 必须与原文一致
- **运行时路径零 LLM**

第三条最好验证, 所以有一条测试用子进程检查运行时不引用本模块。另外「原文可溯」
是这整套东西的价值所在 —— 判据错了没人会在产线上核对, 所以它写成拒绝而不是
警告, 并有专门的抗绕过测试。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from llm_draft_annotations import (  # noqa: E402
    MIN_TRACE_LEN,
    _parse_llm_json,
    draft_prompt,
    proposals_dir,
    rows_needing_annotation,
    traceable,
    validate_draft,
    value_in_doc,
)

KINDS = ("input_voltage", "output_voltage", "efficiency")
DOC = "在标称输入电压下测试, 输出电压应满足 54V±2%, 整机效率不低于 86%。"


class TestTraceability:
    def test_text_in_document_is_traceable(self):
        assert traceable("在标称输入电压下", DOC)

    def test_invented_text_is_not_traceable(self):
        """编造的判据必须被拒 —— 这是整套东西唯一不可让步的一条。"""
        assert not traceable("在 25 摄氏度且满载条件下复测三次", DOC)

    def test_short_fragments_are_never_traceable(self):
        """太短的片段在任何技术文档里都能找到, 溯源通过等于没检查。"""
        assert not traceable(">=5V", DOC)
        assert not traceable("54V", DOC)
        assert not traceable("", DOC)
        assert MIN_TRACE_LEN >= 6

    def test_punctuation_and_whitespace_do_not_block(self):
        """原文单元格里常有标点, LLM 抄写时可能去掉 —— 语义没变不该误杀。"""
        assert traceable("54V±2%", DOC)
        assert traceable("在标称输入电压下，测试", DOC)

    def test_no_fuzzy_matching(self):
        """相似不等于支撑。相近的措辞必须判不可溯。

        「看着像」正是编造最隐蔽的形态: 它能通过大多数看起来合理的检查。
        """
        assert not traceable("在标称输入电压中", DOC)
        assert not traceable("标称输入电压", DOC) or "在标称输入电压下" in DOC


class TestValueInDocument:
    def test_values_present_in_doc_pass(self):
        assert value_in_doc({"typ": 54}, DOC)
        assert value_in_doc({}, DOC)
        # 逐值核: DOC 里有 54 和 2 (来自 54V±2%), 没有别的数
        assert value_in_doc({"min": 54, "max": 2}, DOC)

    def test_value_absent_from_doc_fails(self):
        assert not value_in_doc({"typ": 99}, DOC)
        assert not value_in_doc({"typ": 54, "max": 999}, DOC)

    def test_nested_value_is_rejected_not_passed(self):
        """嵌套结构没法逐 token 核 —— 拒绝而不是放过。"""
        assert not value_in_doc({"a": [1, 2]}, DOC)
        assert not value_in_doc({"a": {"b": 1}}, DOC)

    def test_serialization_trick_does_not_pass(self):
        """整体序列化里塞解释就能骗过「整体包含」检查 —— 所以逐值核。"""
        assert not value_in_doc({"note": "说明文字不在原文"}, DOC)


class TestValidateDraft:
    def test_all_clauses_traceable_is_accepted(self):
        clean, rejected = validate_draft(
            "R1",
            {
                "input": [
                    {
                        "kind": "input_voltage",
                        "text": "在标称输入电压下",
                        "value": {},
                        "reason": "r",
                    }
                ],
                "output": [
                    {
                        "kind": "output_voltage",
                        "text": "输出电压应满足",
                        "value": {"typ": 54},
                        "reason": "r",
                    }
                ],
            },
            KINDS,
            DOC,
        )
        assert clean is not None
        assert len(clean["input"]) == 1 and len(clean["output"]) == 1
        assert rejected == []

    def test_invented_clause_is_dropped_but_others_survive(self):
        """丢掉编造的那个, 不影响其余 —— 一个条目常有多个子句。"""
        clean, rejected = validate_draft(
            "R1",
            {
                "input": [
                    {
                        "kind": "input_voltage",
                        "text": "在标称输入电压下",
                        "value": {},
                        "reason": "r",
                    },
                    {
                        "kind": "input_voltage",
                        "text": "在冰点条件下复测三次",
                        "value": {},
                        "reason": "编的",
                    },
                ],
                "output": [],
            },
            KINDS,
            DOC,
        )
        assert clean is not None
        assert len(clean["input"]) == 1, "编造的子句没有被丢弃"
        assert rejected and "无法在原文溯源" in rejected[0]

    def test_kind_outside_closed_vocab_is_rejected(self):
        """词表封闭: 自造 kind 的下游无法翻译执行动作。"""
        clean, rejected = validate_draft(
            "R1",
            {
                "input": [
                    {"kind": "made_up_kind", "text": "在标称输入电压下", "value": {}, "reason": "r"}
                ],
                "output": [],
            },
            KINDS,
            DOC,
        )
        assert clean is None
        assert "封闭词表" in rejected[0]

    def test_all_rejected_yields_none(self):
        clean, rejected = validate_draft(
            "R1",
            {
                "input": [
                    {"kind": "input_voltage", "text": "冰点复测三次", "value": {}, "reason": "r"}
                ],
                "output": [],
            },
            KINDS,
            DOC,
        )
        assert clean is None
        assert rejected

    def test_value_mismatch_drops_that_clause(self):
        clean, rejected = validate_draft(
            "R1",
            {
                "input": [
                    {
                        "kind": "input_voltage",
                        "text": "在标称输入电压下",
                        "value": {"typ": 999},
                        "reason": "r",
                    }
                ],
                "output": [],
            },
            KINDS,
            DOC,
        )
        assert clean is None
        assert "与原文不一致" in rejected[0]


class TestParseLLMJSON:
    def test_plain_json(self):
        assert _parse_llm_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert _parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_surrounding_prose(self):
        """模型常在 JSON 前后加话 —— 那不该让整批草案失败。"""
        assert _parse_llm_json('好的，结果如下:\n{"a": 1}\n希望有用。') == {"a": 1}

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            _parse_llm_json("我无法回答这个问题")


class TestCandidateSelection:
    def test_only_rows_without_rule_output_are_candidates(self):
        """规则已切出条件的行不该进草案 —— 人写得比 LLM 快, 且规则结果已生效。"""
        rows = rows_needing_annotation("PA601-D54A")
        assert rows, "PA601 应至少有几条规则切不出的行"
        for r in rows:
            assert r["fingerprint"], "草案候选必须带指纹, 否则文档改版后无法自动失效"
            assert r["req_id"]

    def test_candidate_has_real_name_not_none(self):
        """条目名取 title —— 只读 requirement_text 会让三条候选全叫 None。"""
        for r in rows_needing_annotation("PA601-D54A"):
            name = r["title"] or r["requirement_text"] or r["subject"]
            assert name and name != "None", f"{r['req_id']} 条目名为空"

    def test_prompt_includes_the_facts_llm_needs(self):
        """prompt 少给一项, 模型就只能编 —— 所以这几项必须在。"""
        row = {
            "req_id": "R1",
            "section_path": "4.3.1",
            "title": "输入工作电压范围",
            "requirement_text": "",
            "subject": "",
            "notes": "备注原文",
            "min": None,
            "typ": 54,
            "max": None,
            "unit": "Vdc",
        }
        p = draft_prompt(row, KINDS)
        assert "R1" in p and "4.3.1" in p
        assert "输入工作电压范围" in p
        assert "备注原文" in p
        for k in KINDS:
            assert k in p, f"封闭词表 {k} 没进 prompt -> 模型会自造"

    def test_prompt_says_name_may_be_absent(self):
        """条目名全空时要说出来, 而不是发一个空串让模型自由发挥。"""
        p = draft_prompt(
            {
                "req_id": "R",
                "section_path": "1",
                "title": "",
                "requirement_text": "",
                "subject": "",
                "notes": "",
                "min": None,
                "typ": None,
                "max": None,
                "unit": "",
            },
            KINDS,
        )
        assert "无条目名" in p


class TestCandidateSelectionIsReal:
    """选材规则必须有测试 —— 它决定 LLM 被派去做什么活。

    若不排除「规则已切出条件」的行, LLM 会去覆盖已经生效的规则结果: 人写得比
    LLM 快, 且规则版本受回归保护, 而 LLM 版本不是。这类覆盖是无谓的风险。
    """

    def test_rules_already_covering_a_row_keep_it_out(self, monkeypatch):
        import llm_draft_annotations as m

        called = {"n": 0}

        class FakeAssembly:
            def __init__(self, has: bool) -> None:
                self.inputs = ["i"] if has else []
                self.outputs = ["o"] if has else []

        def fake_assemble(row, **kw):
            called["n"] += 1
            # 第一行规则已切出, 第二行没有
            return FakeAssembly(has=called["n"] == 1)

        monkeypatch.setattr(m, "assemble", fake_assemble)
        monkeypatch.setattr(
            m,
            "load_blocks",
            lambda *a, **k: [],
        )
        monkeypatch.setattr(
            m,
            "select_sections",
            lambda blocks, kws: type("S", (), {"section_prefixes": ()})(),
        )
        monkeypatch.setattr(
            m,
            "rows_from_blocks",
            lambda *a, **k: (
                [{"req_id": "R1", "section_path": "1"}, {"req_id": "R2", "section_path": "1"}],
                None,
                None,
            ),
        )
        monkeypatch.setattr(m, "apply_sieve", lambda rows, words: type("A", (), {"kept": rows})())
        out = m.rows_needing_annotation("M1")
        assert [r["req_id"] for r in out] == ["R2"], "规则已覆盖的行被送进了 LLM"


class TestDraftAllPartialRejection:
    """``validate_draft`` 与 ``draft_all`` 要对「部分子句被拒」给出一致的判定。

    只测纯函数不够: 调用方还有一层分支, 那层才是真正决定「丢整条还是留其余」的
    地方, 而它曾经完全没有测试。
    """

    def test_partial_rejection_keeps_the_entry_with_reasons(self, monkeypatch, tmp_path, capsys):
        import llm_draft_annotations as m

        monkeypatch.setattr(m, "proposals_dir", lambda: tmp_path)
        monkeypatch.setattr(m, "_doc_text", lambda _m: DOC)
        monkeypatch.setattr(
            m,
            "rows_needing_annotation",
            lambda *a, **k: [
                {
                    "req_id": "R1",
                    "fingerprint": "f1",
                    "section_path": "1",
                    "title": "输入工作电压范围",
                    "requirement_text": "",
                    "subject": "",
                    "notes": "",
                    "rail": "",
                    "min": None,
                    "typ": None,
                    "max": None,
                    "unit": "",
                    "priority": "",
                }
            ],
        )

        class FakeClient:
            def __init__(self, *a, **k) -> None:
                pass

            async def chat(self, messages, **k):
                # 两个子句: 一个可溯源, 一个编造
                return json.dumps(
                    {
                        "input": [
                            {
                                "kind": "input_voltage",
                                "text": "在标称输入电压下",
                                "value": {},
                                "reason": "r",
                            },
                            {
                                "kind": "input_voltage",
                                "text": "在冰点条件下复测三次",
                                "value": {},
                                "reason": "编的",
                            },
                        ],
                        "output": [],
                        "overall_reason": "验收",
                    }
                )

        import aterag.models as models

        models.LLMClient = FakeClient
        try:
            stats = m.draft_all("M1", dry_run=False)
        finally:
            import importlib

            models.LLMClient = importlib.import_module("aterag.models.llm_client").LLMClient

        out = capsys.readouterr().out
        assert stats["accepted"] == 1, "部分子句被拒不该丢整条"
        assert "丢弃" in out, f"丢弃的子句没有提示: {out}"
        p = tmp_path / "M1.proposals.yaml"
        assert p.exists()
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert len(doc["entries"]["R1"]["input"]) == 1, "编造的子句进了草案"
        assert "无法在原文溯源" in doc["_proposal"]["rejected_clauses"]["R1"][0]

    def test_fully_rejected_entry_is_not_written(self, monkeypatch, tmp_path, capsys):
        import llm_draft_annotations as m

        monkeypatch.setattr(m, "proposals_dir", lambda: tmp_path)
        monkeypatch.setattr(m, "_doc_text", lambda _m: DOC)
        monkeypatch.setattr(
            m,
            "rows_needing_annotation",
            lambda *a, **k: [
                {
                    "req_id": "R1",
                    "fingerprint": "f",
                    "section_path": "1",
                    "title": "t",
                    "requirement_text": "",
                    "subject": "",
                    "notes": "",
                    "rail": "",
                    "min": None,
                    "typ": None,
                    "max": None,
                    "unit": "",
                    "priority": "",
                }
            ],
        )

        class FakeClient:
            def __init__(self, *a, **k) -> None:
                pass

            async def chat(self, messages, **k):
                return json.dumps(
                    {
                        "input": [
                            {
                                "kind": "input_voltage",
                                "text": "冰点复测三次",
                                "value": {},
                                "reason": "r",
                            }
                        ],
                        "output": [],
                    }
                )

        import aterag.models as models

        models.LLMClient = FakeClient
        try:
            stats = m.draft_all("M1", dry_run=False)
        finally:
            import importlib

            models.LLMClient = importlib.import_module("aterag.models.llm_client").LLMClient

        assert stats["accepted"] == 0 and stats["rejected"] == 1
        out = capsys.readouterr().out
        assert "不可溯源" in out
        assert not (tmp_path / "M1.proposals.yaml").exists(), "全部被拒的条目不该产出提案文件"

    def test_single_llm_failure_does_not_abort_the_batch(self, monkeypatch, tmp_path, capsys):
        """一条失败不该中断整批 —— 否则第 3 条模型抽风就丢掉前 2 条的成果。"""
        import llm_draft_annotations as m

        monkeypatch.setattr(m, "proposals_dir", lambda: tmp_path)
        monkeypatch.setattr(m, "_doc_text", lambda _m: DOC)
        rows = [
            {
                "req_id": f"R{i}",
                "fingerprint": f"f{i}",
                "section_path": "1",
                "title": "输入工作电压范围",
                "requirement_text": "",
                "subject": "",
                "notes": "",
                "rail": "",
                "min": None,
                "typ": None,
                "max": None,
                "unit": "",
                "priority": "",
            }
            for i in range(3)
        ]
        monkeypatch.setattr(m, "rows_needing_annotation", lambda *a, **k: rows)

        class FlakyClient:
            def __init__(self, *a, **k) -> None:
                self.calls = 0

            async def chat(self, messages, **k):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("rate limited")
                return json.dumps(
                    {
                        "input": [
                            {
                                "kind": "input_voltage",
                                "text": "在标称输入电压下",
                                "value": {},
                                "reason": "r",
                            }
                        ],
                        "output": [],
                    }
                )

        import aterag.models as models

        models.LLMClient = FlakyClient
        try:
            stats = m.draft_all("M1", dry_run=False)
        finally:
            import importlib

            models.LLMClient = importlib.import_module("aterag.models.llm_client").LLMClient

        assert stats["accepted"] == 2, "其余两条的成果被一条失败带走了"
        assert stats["errors"] == 1
        doc = yaml.safe_load((tmp_path / "M1.proposals.yaml").read_text(encoding="utf-8"))
        assert set(doc["entries"]) == {"R0", "R2"}
        assert "rate limited" in doc["_proposal"]["rejected_clauses"]["R1"][0]


class TestNoLLMAtRuntime:
    def test_runtime_does_not_import_the_drafter(self):
        """运行时路径零 LLM —— 这条不破。

        用子进程验证: 导入抽取全路径后, drafts 模块不该出现在 sys.modules 里。
        """
        code = (
            "import sys; sys.path.insert(0, 'src');"
            "import aterag.extract.api, aterag.extract.configs, aterag.checks, aterag.rag.service;"
            "bad = [m for m in sys.modules if 'llm_draft' in m];"
            "assert not bad, bad"
        )
        p = subprocess.run(
            [str(ROOT / ".venv/Scripts/python.exe"), "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=ROOT,
        )
        assert p.returncode == 0, p.stdout + p.stderr

    def test_runtime_does_not_read_the_proposals_dir(self):
        """提案目录不参与运行期 —— 否则一条注记就有两处可改 (红线 4)。"""
        hits = subprocess.run(
            ["git", "grep", "-n", "proposals", "--", "src/"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=ROOT,
        )
        assert hits.returncode != 0 or not hits.stdout.strip(), (
            "src/ 里出现了对 proposals 的引用:\n" + hits.stdout
        )

    def test_proposals_dir_is_under_annotations_not_config(self):
        """提案属运行时数据不进版本库 (客户判据 + 需求编号)。"""
        d = proposals_dir()
        assert "annotations" in str(d).replace("\\", "/"), str(d)


class TestDraftFileShape:
    def test_accepted_proposals_are_written_as_draft(self, tmp_path, monkeypatch):
        """草案绝不带 approved —— 签字只能由 review_annotation --approve 产生。"""
        import llm_draft_annotations as m

        monkeypatch.setattr(m, "proposals_dir", lambda: tmp_path)
        p = m.write_proposals(
            "M1",
            {"R1": {"status": "approved", "approved_by": "伪造", "input": [], "output": []}},
            rejected={},
        )
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        # 写出来是 approved 也不对 —— 所以断言这里必须标 pending
        assert doc["_proposal"]["status"] == "待人工评审"
        assert "未经人签字" in doc["_proposal"]["warning"]

    def test_rejected_reasons_are_recorded(self, tmp_path, monkeypatch):
        """被丢弃的子句要留下理由 —— 否则人以为草案是完整的。"""
        import llm_draft_annotations as m

        monkeypatch.setattr(m, "proposals_dir", lambda: tmp_path)
        p = m.write_proposals("M1", {}, rejected={"R9": ["text 无法在原文溯源: 编的"]})
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert "无法在原文溯源" in doc["_proposal"]["rejected_clauses"]["R9"][0]

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        import llm_draft_annotations as m

        monkeypatch.setattr(m, "proposals_dir", lambda: tmp_path)
        monkeypatch.setattr(m, "rows_needing_annotation", lambda *a, **k: [])
        stats = m.draft_all("M1", dry_run=True)
        assert stats["candidates"] == 0
        assert not list(tmp_path.iterdir()), "dry-run 落了盘"

    def test_no_doc_means_no_drafts(self, tmp_path, monkeypatch, capsys):
        """读不到原文时一个草案都不出。

        LLM 起草的全部价值在「原文可溯」这条红线上; 没有原文就只能批量造判据。
        """
        import llm_draft_annotations as m

        monkeypatch.setattr(m, "_doc_text", lambda _m: "")
        monkeypatch.setattr(
            m,
            "rows_needing_annotation",
            lambda *a, **k: [
                {
                    "req_id": "R1",
                    "fingerprint": "f",
                    "section_path": "1",
                    "title": "t",
                    "requirement_text": "",
                    "subject": "",
                    "notes": "",
                    "rail": "",
                    "min": None,
                    "typ": None,
                    "max": None,
                    "unit": "",
                    "priority": "",
                }
            ],
        )
        stats = m.draft_all("M1", dry_run=True)
        out = capsys.readouterr().out
        assert stats["accepted"] == 0
        assert "无法溯源" in out


class TestAcceptProposals:
    def test_merge_forces_draft_and_clears_signature(self, tmp_path, monkeypatch):
        """合并一律 draft —— LLM 起草 + 自动签字 = 编造的判据一路绿灯进产线。"""
        import importlib

        ra = importlib.import_module("review_annotation")

        target = tmp_path / "M1.conditions.yaml"
        target.write_text(
            yaml.safe_dump(
                {"version": 1, "entries": {"R0": {"status": "approved", "approved_by": "张三"}}},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        (tmp_path / "proposals").mkdir()
        (tmp_path / "proposals" / "M1.proposals.yaml").write_text(
            yaml.safe_dump(
                {
                    "entries": {
                        "R1": {
                            "status": "approved",
                            "approved_by": "伪造",
                            "input": [{"kind": "input_voltage"}],
                            "output": [],
                        },
                        "R0": {"status": "draft", "input": [], "output": []},
                    }
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        import aterag.config as cfg

        s = cfg.Settings()
        s.annotations_dir = str(tmp_path)
        monkeypatch.setattr(ra, "get_settings", lambda: s)

        assert ra.cmd_accept_proposals("M1") == 0
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
        r1 = doc["entries"]["R1"]
        assert r1["status"] == "draft", "合并时把 approved 带进来了"
        assert "approved_by" not in r1
        # 已签字的条目不得被草案覆盖
        assert doc["entries"]["R0"]["status"] == "approved"
        assert doc["entries"]["R0"].get("approved_by") == "张三"

    def test_missing_proposals_file_is_an_error(self, tmp_path, monkeypatch, capsys):
        import importlib

        import aterag.config as cfg

        ra = importlib.import_module("review_annotation")
        s = cfg.Settings()
        s.annotations_dir = str(tmp_path)
        monkeypatch.setattr(ra, "get_settings", lambda: s)
        assert ra.cmd_accept_proposals("M1") == 1
        assert "没有提案文件" in capsys.readouterr().out
