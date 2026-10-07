"""模板画像生成器 (方案 §11.6) 的不变量测试。

定位说明 (决定了这些断言该有多严): 这是**辅助工具, 不承担检测职责**。检测归
抽取过程的 fail-closed; 它坏了只影响方便, 不影响正确性。所以测试钉的是「不
误导」与「预检真的能提前抓到 A20 那条报错」, 而不是「它 100% 可靠」。

最要紧的一条是 :func:`precheck_roles` —— 手写完 profile 要跑一遍抽取才知道
role 写没写错, 这个工具存在的全部理由就是把那次报错提前到提案阶段。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aterag.extract.supplement import MethodBook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from table_schema_report import (  # noqa: E402
    build_profile_proposal,
    candidate_keywords,
    precheck_roles,
    role_vocabulary,
    section_tree,
    split_heading,
)

BLOCKS = "rag_storage/blocks/PA601-D54A.jsonl"


@pytest.fixture(scope="module")
def blocks():
    import json

    from aterag.ingest.markdown_parser import Block

    out = []
    for line in Path(BLOCKS).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out.append(
            Block(
                chunk_id=d["chunk_id"],
                heading=d["heading"],
                level=d["level"],
                parent_headings=d.get("parent_headings", []),
                section_path=d.get("section_path", ""),
                text=d.get("text", ""),
                tables=d.get("tables", []),
                is_table_block=d.get("is_table_block", False),
            )
        )
    return out


@pytest.fixture(scope="module")
def methods():
    return MethodBook.load()


def _b(section_path: str, heading: str, level: int = 3, tables: int = 0):
    from aterag.ingest.markdown_parser import Block

    return Block(
        chunk_id=section_path,
        heading=heading,
        level=level,
        section_path=section_path,
        tables=[[["h"] + [""] * 3, ["a"] + [""] * 3] for _ in range(tables)],
    )


def _tree(*specs) -> list[dict]:
    """Block 列表 -> section_tree 的行 (候选关键词吃的是行, 不是 Block)。"""
    return section_tree(list(specs))


class TestSectionTree:
    def test_same_section_is_not_listed_three_times(self):
        """blocks 是**分段**的: 一个章节常有标题块+表格块+正文块, 必须按编号去重。"""
        tree = section_tree([_b("4.3.1", "4.3.1 输入"), _b("4.3.1", "4.3.1 输入"), _b("4.3.2", "4.3.2 输出")])
        assert [r["section_path"] for r in tree] == ["4.3.1", "4.3.2"]

    def test_sorted_by_section_number_not_string(self):
        """按字符串排会把 '4.10' 排在 '4.2' 前面 —— 编号是数, 不是词。"""
        tree = section_tree([_b("4.10", "4.10 十"), _b("4.2", "4.2 二"), _b("4.1", "4.1 一")])
        assert [r["section_path"] for r in tree] == ["4.1", "4.2", "4.10"]

    def test_counts_blocks_and_tables(self):
        tree = section_tree([_b("4.4", "4.4 试验", tables=2), _b("4.4", "4.4 试验")])
        row = tree[0]
        assert row["blocks"] == 2
        assert row["tables"] == 2

    def test_scope_filters_with_dot_boundary(self):
        """scope=4.3 应命中 4.3.1, 不该命中 4.31 —— 前缀相同但不是子章节。"""
        bl = [_b("4.3", "4.3"), _b("4.3.1", "4.3.1"), _b("4.31", "4.31")]
        assert [r["section_path"] for r in section_tree(bl, scope="4.3")] == ["4.3", "4.3.1"]

    def test_real_document_tree_has_the_spec_chapters(self, blocks):
        tree = section_tree(blocks)
        paths = {r["section_path"] for r in tree}
        # PA601 的板级信号表所在章节, 以及功能/性能要求
        assert "4.2.4.3" in paths
        assert any("4.3" == p or p.startswith("4.3.") for p in paths)

    def test_parent_headings_are_in_the_tree(self, blocks):
        """父章节常常只有子章节的块、自己没块 —— 而选择器要的恰恰是父章节。

        实测 PA601 的 `4.3 功能/性能要求` 只出现在子块的 parent_headings 里,
        遍历块看不到它, 而 PA601 档案的 section_keywords 用的就是它。
        """
        tree = section_tree(blocks)
        by_path = {r["section_path"]: r for r in tree}
        assert "4.3" in by_path, "父章节 4.3 应出现在章节树里"
        assert by_path["4.3"]["heading"] == "4.3 功能/性能要求"
        assert by_path["4.3"]["from_parent"] is True

    def test_parent_heading_without_own_block_is_not_skipped_by_scope(self, blocks):
        """scope 限定的是**目标章节及其子树**, 祖先章节仍要给出 (关键词往往在祖先上)。"""
        tree = section_tree(blocks, scope="4.3.1")
        paths = {r["section_path"] for r in tree}
        assert "4.3.1" in paths
        assert "4.3" in paths, "祖先章节被 scope 过滤掉了 -> 候选关键词会漏掉它"


class TestSplitHeading:
    def test_strips_section_number(self):
        assert split_heading("4.3.1 保护功能") == ("4.3.1", "保护功能")

    def test_strips_requirement_id(self):
        """PA601 标题带实例编号; 换产品就变, 而关键词要跨文档稳定。"""
        assert split_heading("4.2.1 SR-PA601-D54A-0300 结构要求") == ("4.2.1", "结构要求")

    def test_both_stripped(self):
        assert split_heading("4.2.1 SR-PA601-D54A-0300 结构要求")[1] == "结构要求"
        assert split_heading("4.3 功能/性能要求")[1] == "功能/性能要求"

    def test_keeps_slashes_and_symbols_in_title(self):
        """标题里的 '/' 是语义的一部分, 剥掉就选不中了。"""
        assert split_heading("4.3 功能/性能要求")[1] == "功能/性能要求"
        assert split_heading("4.5 电压/电流精度")[1] == "电压/电流精度"

    def test_unnumbered_heading_kept_whole(self):
        assert split_heading("目录") == ("", "目录")

    def test_empty_input(self):
        assert split_heading("") == ("", "")


class TestCandidateKeywords:
    def test_strips_section_number(self):
        kws = candidate_keywords(_tree(_b("4.3.1", "4.3.1 保护功能")))
        assert kws[0]["keyword"] == "保护功能"

    def test_carries_provenance(self):
        """候选必须带出处与表数 —— 不然人得回文档里逐条查。"""
        kws = candidate_keywords(_tree(_b("4.3.1", "4.3.1 保护功能", tables=3)))
        assert kws[0]["sections"] == ["4.3.1"]
        assert kws[0]["tables"] == 3

    def test_table_bearing_keywords_rank_first(self):
        """章节选择器多半要选装表的那种 —— 有表的排前面。"""
        kws = candidate_keywords(
            _tree(_b("1", "1 范围和目的"), _b("4.3", "4.3 功能/性能要求", tables=5))
        )
        assert kws[0]["keyword"] == "功能/性能要求"

    def test_real_document_has_the_keyword_profiles_actually_use(self, blocks):
        """PA601 的档案用的就是「功能/性能要求」—— 候选表里必须出现它。"""
        kws = candidate_keywords(section_tree(blocks))
        assert "功能/性能要求" in {k["keyword"] for k in kws}


class TestRolePrecheck:
    def test_referenced_roles_exclude_builtin_instructions(self, methods):
        """any / any_except_other 是匹配指令不是角色名, 不能出现在候选角色里。"""
        referenced, builtin = role_vocabulary(methods)
        assert "any" not in referenced and "any_except_other" not in referenced
        assert set(builtin) == {"any", "any_except_other"}

    def test_referenced_roles_are_non_empty(self, methods):
        """本仓库的方法库确实引用了具体角色 —— 否则这条预检在空转。"""
        referenced, _ = role_vocabulary(methods)
        assert referenced, "方法库没有任何 role 引用, 预检将永远报 ok=True"

    def test_missing_role_is_reported_as_uncovered(self, methods):
        """这就是 A20 那条 ValueError 的提前版: 提案漏声明 -> 抽取会抛。"""
        pc = precheck_roles({"4.3.1": "output_spec"}, methods)
        assert not pc["ok"]
        assert set(pc["uncovered"]) == set(pc["referenced_by_methods"]) - {"output_spec"}

    def test_covering_all_referenced_roles_passes(self, methods):
        referenced, _ = role_vocabulary(methods)
        priors = {f"4.3.{i + 1}": r for i, r in enumerate(referenced)}
        pc = precheck_roles(priors, methods)
        assert pc["ok"], f"应通过却没过: {pc}"
        assert pc["uncovered"] == []

    def test_empty_proposal_does_not_pass(self, methods):
        """没填 role 就想通过? 那正是原来"跑抽取才发现"的场景。"""
        assert not precheck_roles({}, methods)["ok"]

    def test_unused_roles_are_reported_but_not_fatal(self, methods):
        """声明了没有方法引用的角色无害 —— 只提一句, 不该让提案判失败。"""
        pc = precheck_roles({"4.3.1": "output_spec", "9.9.9": "no_such_role_used"}, methods)
        assert "no_such_role_used" in pc["unused"]
        assert pc["uncovered"]

    def test_note_points_at_the_real_failure_mode(self, methods):
        """报错文案要指向真实后果 (加载期 ValueError), 否则人不知道该做什么。"""
        pc = precheck_roles({}, methods)
        assert "ValueError" in pc["note"] and "section_priors" in pc["note"]


@pytest.fixture(scope="module")
def deterministic_doc(blocks, methods):
    from aterag.ingest.table_schema import load_registry

    return build_profile_proposal(blocks, {}, load_registry("config/table_schemas.yaml"), methods, use_llm=False)


class TestProposalDocument:
    def test_marked_as_pending_review(self, deterministic_doc):
        """红线 9: 草案不进运行时。状态必须显式写明待人工审核。"""
        assert deterministic_doc["_proposal"]["status"] == "待人工审核"

    def test_states_it_is_not_the_effective_config(self, deterministic_doc):
        """提案落在 proposals/, 生效配置是 config/ —— 文件里要写死这点。"""
        assert "doc_profiles.yaml" in deterministic_doc["usage_note"]

    def test_deterministic_parts_present_without_llm(self, deterministic_doc):
        """不加 --llm 也要给出章节树/关键词/表头签名 —— 那部分本来就是确定的。"""
        assert deterministic_doc["section_tree"]
        assert deterministic_doc["candidate_section_keywords"]
        assert "role_precheck" in deterministic_doc

    def test_without_llm_priors_are_empty_not_guessed(self, deterministic_doc):
        """没有 LLM 就不猜 role: 猜错比不猜更坏 (并进去就会抛)。"""
        assert deterministic_doc["section_priors"] == {}
        assert "--llm" in deterministic_doc["_proposal"]["note"]

    def test_serializes_to_yaml(self, deterministic_doc):
        import yaml

        back = yaml.safe_load(yaml.safe_dump(deterministic_doc, allow_unicode=True))
        assert back["_proposal"]["status"] == "待人工审核"

    def test_llm_keywords_not_in_document_are_rejected_not_trusted(self, blocks, methods):
        """LLM 提一个实际不存在的章节名 -> 收下它就等于章节选择器永远选不中。"""
        import aterag.models as models

        class FakeLLM:
            def __init__(self, *a, **k):
                pass

            async def chat(self, messages, **k):
                return (
                    '{"priors": [{"section": "4.3.1", "role": "output_spec", '
                    '"reason": "输出表"}], "keywords": ["根本不存在的章节", "功能/性能要求"]}'
                )

        orig = models.LLMClient
        models.LLMClient = FakeLLM
        try:
            from aterag.ingest.table_schema import load_registry

            reg = load_registry("config/table_schemas.yaml")
            doc = build_profile_proposal(blocks, {}, reg, methods, use_llm=True)
        finally:
            models.LLMClient = orig
        assert "根本不存在的章节" in doc["_proposal"]["llm_keywords_rejected"]
        assert "功能/性能要求" in {k["keyword"] for k in doc["candidate_section_keywords"]}

    def test_llm_role_outside_vocabulary_is_dropped(self, blocks, methods):
        """LLM 提一个词表外的 role -> 必须丢, 并计入 tried/accepted 的差。"""
        import aterag.models as models

        class FakeLLM:
            def __init__(self, *a, **k):
                pass

            async def chat(self, messages, **k):
                return (
                    '{"priors": [{"section": "4.3.1", "role": "made_up_role", '
                    '"reason": "x"}, {"section": "4.2.1", "role": "input_domain", "reason": "y"}]}'
                )

        orig = models.LLMClient
        models.LLMClient = FakeLLM
        try:
            from aterag.ingest.table_schema import load_registry

            reg = load_registry("config/table_schemas.yaml")
            doc = build_profile_proposal(blocks, {}, reg, methods, use_llm=True)
        finally:
            models.LLMClient = orig
        assert set(doc["section_priors"]) == {"4.2.1"}, "词表外的 role 必须被丢弃"
        assert doc["_proposal"]["llm"]["tried_priors"] == 2
        assert doc["_proposal"]["llm"]["accepted_priors"] == 1

    def test_llm_failure_does_not_lose_deterministic_parts(self, blocks, methods):
        """LLM 挂了只影响语义建议 —— 章节树/关键词/预检必须照常给出。"""
        import aterag.models as models

        class BoomLLM:
            def __init__(self, *a, **k):
                pass

            async def chat(self, *a, **k):
                raise RuntimeError("no credentials")

        orig = models.LLMClient
        models.LLMClient = BoomLLM
        try:
            from aterag.ingest.table_schema import load_registry

            reg = load_registry("config/table_schemas.yaml")
            doc = build_profile_proposal(blocks, {}, reg, methods, use_llm=True)
        finally:
            models.LLMClient = orig
        assert "error" in doc["_proposal"]["llm"]
        assert doc["section_tree"], "LLM 失败不应带走确定性部分"
        assert "role_precheck" in doc


def _profile_without_role(profile, role: str):
    """复制一份档案, 删掉所有 role == 该值的 section_priors 条目。"""
    from dataclasses import replace

    priors = {
        sec: (replace(pr, role="other") if pr.role == role else pr)
        for sec, pr in profile.section_priors.items()
    }
    return replace(profile, section_priors=priors)


class TestPrecheckMatchesRealFailure:
    """预检与真正会抛的那条报错必须同判 —— 否则工具在骗人。

    这是本工具存在的**全部理由**: 提案阶段说"ok", 抽取时却抛
    ``ValueError: methods[x].applies_to.role 非法``, 那比没有工具更坏。
    """

    def test_repository_profiles_pass_the_precheck(self):
        from aterag.extract.api import ProfileBook
        from aterag.extract.configs import role_vocabulary

        pb = ProfileBook.load("config/doc_profiles.yaml")
        cand = {
            f"{p.name}.{sec}": pr.role
            for p in pb.profiles.values()
            for sec, pr in p.section_priors.items()
        }
        pc = precheck_roles(cand, methods := MethodBook.load())
        assert pc["ok"], f"现行档案的角色组合应通过预检: {pc}"
        # 5 个约定角色里, 方法库只引用了 3 个 —— 其余是无方法引用的角色。
        assert set(pc["unused"]) == {"other", "protection_response"}
        assert set(role_vocabulary(pb)) == {
            "input_domain",
            "other",
            "output_spec",
            "protection_response",
            "signal_io",
        }
        assert methods.methods, "前置: 方法库不应为空"

    def test_dropping_one_role_makes_precheck_fail(self):
        """少声明一个角色 -> 预检必须变红, 且点的正是方法库引用的那个。"""
        from aterag.extract.api import ProfileBook

        pb = ProfileBook.load("config/doc_profiles.yaml")
        full = {
            f"{p.name}.{sec}": pr.role
            for p in pb.profiles.values()
            for sec, pr in p.section_priors.items()
        }
        mb = MethodBook.load()
        referenced = precheck_roles(full, mb)["referenced_by_methods"]
        dropped = referenced[0]
        partial = {k: v for k, v in full.items() if v != dropped}
        pc = precheck_roles(partial, mb)
        assert not pc["ok"]
        assert pc["uncovered"] == [dropped]

    def test_precheck_and_real_validator_agree_on_the_broken_case(self):
        """预检说不行时, 真抽取的加载期校验也必须不行 —— 两边不能各说各话。"""
        from aterag.extract.api import ProfileBook
        from aterag.extract.assembler import PatternBook
        from aterag.extract.assess import RuleBook
        from aterag.extract.configs import validate_extraction_configs
        from aterag.extract.scenarios import ScenarioRules

        pb = ProfileBook.load("config/doc_profiles.yaml")
        referenced = precheck_roles({}, MethodBook.load())["referenced_by_methods"]
        partial = {
            f"{p.name}.{sec}": pr.role
            for p in pb.profiles.values()
            for sec, pr in p.section_priors.items()
            if pr.role != referenced[0]
        }
        assert not precheck_roles(partial, MethodBook.load())["ok"]

        # 真校验: 用一份缺 role 的档案书 -> 必须抛
        dropped = {
            n: _profile_without_role(p, referenced[0])
            for n, p in pb.profiles.items()
        }
        broken = ProfileBook(
            profiles=dropped,
            default_profile=pb.default_profile,
            source_path=pb.source_path,
        )
        try:
            validate_extraction_configs(
                broken,
                PatternBook.load("config/condition_patterns.yaml"),
                MethodBook.load(),
                RuleBook.load("config/test_methods.yaml"),
                ScenarioRules.load(),
            )
        except ValueError as e:
            assert referenced[0] in str(e), f"真校验没点名缺失角色: {e}"
        else:
            raise AssertionError("真校验竟然通过了 -> 预检与真报错不同判")


class TestNoRuntimeImpact:
    def test_script_is_not_imported_by_runtime(self):
        """它是辅助工具: 运行期不许 import 它, 否则"坏了不影响正确性"就是空话。"""
        import subprocess

        code = (
            "import sys; sys.path.insert(0, 'src');"
            "import aterag.extract.api, aterag.checks, aterag.extract.configs;"
            "assert 'table_schema_report' not in sys.modules, '运行时引入了辅助工具'"
        )
        p = subprocess.run(
            [".venv/Scripts/python.exe", "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert p.returncode == 0, p.stdout + p.stderr
