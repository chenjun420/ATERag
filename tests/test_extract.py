"""抽取核心单测 (CI 可跑, 不依赖 PG/Qdrant/LightRAG).

只测纯逻辑: 章节选择 / 剔除规则 / 短横线语义 / 条件装配 / 表结构识别。
需要真实存储栈的验证在 scripts/verify_conditions.py (走 blocks 侧车, 离线可跑)。
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "src")

from aterag.extract import (
    ModelNotIngested,
    PatternBook,
    ProfileBook,
    SectionKeywordNotFound,
    apply_sieve,
    is_placeholder,
    row_fingerprint,
    section_matches,
    select_sections,
)
from aterag.extract.api import DocProfile, SectionPrior
from aterag.extract.assembler import assemble
from aterag.ingest.entity_extract import extract_from_blocks
from aterag.ingest.markdown_parser import parse_markdown
from aterag.ingest.table_schema import infer_roles, load_registry

# ---------------- 最小规格书样本 (自包含, 不依赖仓库文件) ----------------

SAMPLE = """# 测试电源规格书

## 1 范围

略。

## 4 技术要求

### 4.1 环境条件

#### 4.1.1 工作环境

| 编号 | 项目 | 单位 | 最小值 | 典型值 | 最大值 | 备注 | 等级 |
|---|---|---|---|---|---|---|---|
| SR-X-0100 | 工作温度范围 | ℃ | -40 | 25 | 70 | 长期工作 | 强制 |

## 4.3 功能/性能要求

### 4.3.1 输入特性

| 编号 | 项目 | 单位 | 最小值 | 典型值 | 最大值 | 备注 | 等级 |
|---|---|---|---|---|---|---|---|
| SR-X-1100 | 输入工作电压范围 | Vac | 88 | 110 | 290 | 长期工作。 | 强制 |
| SR-X-1101 | 输入防反功能 | - | - | - | - | 输入反接后可以不工作，但不能损坏 | 不要求 |

### 4.3.2 输出特性

| 编号 | 项目 | 单位 | 最小值 | 典型值 | 最大值 | 备注 | 等级 |
|---|---|---|---|---|---|---|---|
| SR-X-1200 | 额定输出电压 | V | -54.8 | -54 | -53.2 | 上电默认输出 | 强制 |
| SR-X-1210 | 整机效率 | % | 86 | - | - | 额定220Vac 输入，50%最大输出负载。 | 强制 |
| SR-X-1210 | 整机效率 | % | 91 | - | - | 额定220Vac 输入，100%最大输出负载。 | 强制 |
| SR-X-1220 | 输出电压可调节范围 | V | - | - | - | 不要求 | 不要求 |
| SR-X-1230 | 温度系数 | %/℃ | - | - | - | - | 强制 |

### 4.3.3 保护功能

| 编号 | 项目 | 单位 | 最小值 | 典型值 | 最大值 | 备注 | 等级 |
|---|---|---|---|---|---|---|---|
| SR-X-1300 | 输出过流保护点 | A | 12 | - | 18 | 动作后切断输出 | 强制 |
"""

# 对齐 4.1.x / 4.2.x / 4.4.x 的表格, 验证章节边界不会被误收
SAMPLE_OUTSIDE = """
### 4.2.4.2 输出接口

| 连接器代码 | 名称 | 型号 | 品牌或供应商 | 联系方式 |
|---|---|---|---|---|
| 输出连接器 X2 | 弯式焊接连接器 | PCN-001 | GLGNET | - |

### 4.4.7 底线安全性要求

| 编号 | 项目 | 要求 | 等级 |
|---|---|---|---|
| SR-X-4001 | 底线保护 | 需具备 | 强制 |
"""


@pytest.fixture
def blocks():
    return parse_markdown(SAMPLE + SAMPLE_OUTSIDE)


@pytest.fixture
def profile():
    return DocProfile(
        name="t",
        section_keywords=("功能/性能要求",),
        exclude_words=("不要求", "无要求"),
        section_priors={
            "4.3.1": SectionPrior(role="input_domain", limits_to="input"),
            "4.3.2": SectionPrior(role="output_spec", limits_to="output"),
            "4.3.3": SectionPrior(role="protection_response", limits_to="both"),
        },
    )


# ---------------- 章节选择 ----------------
class TestSelector:
    def test_hit_and_children(self, blocks):
        sel = select_sections(blocks, ["功能/性能要求"])
        assert sel.matched_headings == ["4.3 功能/性能要求"]
        assert sel.section_prefixes == ["4.3"]

    def test_prefix_boundary(self):
        assert section_matches("4.3.1", ["4.3"]) is True
        assert section_matches("4.3", ["4.3"]) is True
        # 关键: 4.31/4.30 不应被 4.3 收编
        assert section_matches("4.31", ["4.3"]) is False
        assert section_matches("4.30.1", ["4.3"]) is False

    def test_optional_section(self):
        assert section_matches("4.3.4.1", ["4.3"]) is True

    def test_zero_hit_fail_closed(self, blocks):
        with pytest.raises(SectionKeywordNotFound):
            select_sections(blocks, ["根本不存在的章节"])

    def test_empty_keywords_fail_closed(self, blocks):
        with pytest.raises(SectionKeywordNotFound):
            select_sections(blocks, [])

    def test_outside_sections_excluded(self, blocks):
        sel = select_sections(blocks, ["功能/性能要求"])
        picked = {b.section_path for b in blocks if b.chunk_id in sel.selected_chunk_ids}
        assert not any(s.startswith("4.2") for s in picked)
        assert not any(s.startswith("4.4") for s in picked)


# ---------------- 剔除规则 ----------------
class TestContract:
    """契约: 被过滤的需求不参与条件抽取; 抽出的需求必须已装配条件。"""

    def test_excluded_never_reaches_conditions(self, blocks, profile):
        from aterag.extract.api import rows_from_blocks

        sel = select_sections(blocks, profile.section_keywords)
        rows, _, _ = rows_from_blocks(blocks, "TST", "A", sel.section_prefixes)
        out = apply_sieve(rows, profile.exclude_words)
        excl_ids = {e.req_id for e in out.excluded}
        kept_ids = {r["req_id"] for r in out.kept}
        # 不要求的需求既不产条件, 也不会与保留集重叠
        assert "SR-X-1101" in excl_ids
        assert "SR-X-1101" not in kept_ids
        assert not (excl_ids & kept_ids)

    def test_kept_rows_all_assembled(self, profile):
        from aterag.extract.assembler import assemble

        book = PatternBook.load()
        rows = [
            {"req_id": "X-1", "title": "输出电压", "min": -54.0, "max": -53.2, "unit": "V"},
            {"req_id": "X-2", "title": "效率", "min": 91.0, "unit": "%", "notes": "额定220Vac输入"},
        ]
        for r in rows:
            prior = profile.prior_for("4.3.2")
            asm = assemble(
                r, role=prior.role, limits_to=prior.limits_to, book=book, annotations=None
            )
            # 每个抽出的需求都必须切出至少一侧条件, 否则应进待审而非静默产出空条件
            assert asm.inputs or asm.outputs, f"{r['req_id']} 未切出任何条件"
            assert all(c.text for c in (*asm.inputs, *asm.outputs)), "子句不得为空文本"

    def test_clause_text_never_empty(self, profile):
        """R3 精神: 不臆造条件 —— 子句必须有原文依据, 不能是空壳。"""
        from aterag.extract.assembler import assemble

        book = PatternBook.load()
        prior = profile.prior_for("4.3.2")
        for r in (
            {"req_id": "Y-1", "title": "空行", "min": None, "max": None, "notes": "-"},
            {"req_id": "Y-2", "title": "有值", "min": 5.0, "unit": "V", "notes": "-"},
        ):
            asm = assemble(
                r, role=prior.role, limits_to=prior.limits_to, book=book, annotations=None
            )
            for c in (*asm.inputs, *asm.outputs):
                assert c.text.strip(), "子句文本为空 = 臆造"
                assert c.kind in book.kinds


class TestSieve:
    def test_priority_excluded(self):
        out = apply_sieve(
            [{"req_id": "A", "priority": "不要求", "notes": "-", "section_path": "4.3.1"}],
            ("不要求",),
        )
        assert len(out.kept) == 0
        assert out.excluded[0].field == "priority"
        assert out.excluded[0].matched_word == "不要求"

    def test_notes_excluded(self):
        """等级无剔除词、备注整格=剔除词 -> R2 兜底剔除 (字段记为 notes)."""
        out = apply_sieve(
            [{"req_id": "B", "priority": "强制", "notes": "无要求", "section_path": "4.3.1"}],
            ("不要求", "无要求"),
        )
        assert len(out.kept) == 0
        assert out.excluded[0].field == "notes"
        assert out.excluded[0].matched_word == "无要求"

    def test_substring_does_not_exclude(self):
        """备注含"不要求"子串但等级=强制 -> 必须保留 (子串匹配会误删强制项)."""
        out = apply_sieve(
            [
                {
                    "req_id": "C",
                    "priority": "强制",
                    "notes": "不要求均流度，但不能出现并联切换",
                    "section_path": "4.3.2",
                }
            ],
            ("不要求",),
        )
        assert len(out.kept) == 1
        assert len(out.excluded) == 0

    def test_every_exclusion_auditable(self):
        out = apply_sieve(
            [{"req_id": "D", "priority": "不要求", "notes": "-", "section_path": "4.3.1"}],
            ("不要求",),
        )
        e = out.excluded[0]
        assert e.reason and e.matched_word and e.req_id and e.field

    def test_missing_priority_flagged_not_dropped(self):
        out = apply_sieve(
            [{"req_id": "E", "priority": "", "notes": "x", "section_path": "4.3.1"}],
            ("不要求",),
        )
        assert len(out.kept) == 1
        assert out.unclassified_priority == 1


# ---------------- 短横线语义 ----------------
class TestPlaceholder:
    @pytest.mark.parametrize("v", ["-", "—", "", "  -  ", "/"])
    def test_placeholder_recognized(self, v):
        assert is_placeholder(v) is True

    @pytest.mark.parametrize("v", ["0", "-54", "不要求", "A", "0.5"])
    def test_not_placeholder(self, v):
        assert is_placeholder(v) is False

    def test_dash_row_kept_not_excluded(self):
        """等级=强制 + 限值全为 '-' -> 保留, 不剔除 (短横线≠不要求)."""
        out = apply_sieve(
            [
                {
                    "req_id": "F",
                    "priority": "强制",
                    "min": None,
                    "typ": None,
                    "max": None,
                    "notes": "-",
                    "section_path": "4.3.2",
                }
            ],
            ("不要求",),
        )
        assert len(out.kept) == 1
        assert len(out.excluded) == 0

    def test_no_data_dims_recorded(self):
        out = apply_sieve(
            [
                {
                    "req_id": "G",
                    "priority": "强制",
                    "min": None,
                    "max": 50.0,
                    "notes": "-",
                    "section_path": "4.3.2",
                }
            ],
            ("不要求",),
        )
        dims = out.no_data_dims.get("G", [])
        assert "min" in dims
        assert "max" not in dims  # max 有值, 不该记为无数据

    def test_table_structure_not_counted_as_gap(self):
        """遥测表没有 rail 列 -> rail 为空是表结构使然, 不该报成数据缺失."""
        out = apply_sieve(
            [
                {
                    "req_id": "H",
                    "priority": "强制",
                    "min": None,
                    "max": None,
                    "notes": "x",
                    "rail": "",
                    "subject": "输入电压",
                    "range_text": "0~320Vac",
                }
            ],
            ("不要求",),
        )
        dims = out.no_data_dims.get("H", [])
        assert "rail" not in dims
        assert "range_text" not in dims


# ---------------- 条件装配 ----------------
class TestAssembler:
    @pytest.fixture
    def book(self):
        return PatternBook.load()

    def test_limits_to_output(self, book):
        row = {"req_id": "X", "title": "整机效率", "min": 91.0, "unit": "%", "notes": ""}
        asm = assemble(row, role="output_spec", limits_to="output", book=book)
        assert any(o.kind == "efficiency" for o in asm.outputs)
        assert not asm.inputs

    def test_limits_to_input(self, book):
        row = {"req_id": "X", "title": "输入电压范围", "min": 88.0, "max": 290.0, "unit": "Vac"}
        asm = assemble(row, role="input_domain", limits_to="input", book=book)
        assert any(i.kind == "input_voltage" for i in asm.inputs)
        assert not asm.outputs

    def test_protection_both_sides(self, book):
        row = {"req_id": "X", "title": "输出过流保护点", "min": 12.0, "max": 18.0, "unit": "A"}
        asm = assemble(row, role="protection_response", limits_to="both", book=book)
        assert any(i.kind == "fault_stimulus" for i in asm.inputs)
        assert any(o.kind == "protection_action" for o in asm.outputs)

    def test_notes_parsed_into_stimulus(self, book):
        row = {
            "req_id": "X",
            "title": "整机效率",
            "min": 91.0,
            "unit": "%",
            "notes": "额定220Vac 输入，50%最大输出负载。",
        }
        asm = assemble(row, role="output_spec", limits_to="output", book=book)
        kinds = {i.kind for i in asm.inputs}
        assert "input_voltage" in kinds
        assert "load" in kinds
        load = next(i for i in asm.inputs if i.kind == "load")
        assert load.value and load.value.get("value") == 50.0

    def test_dash_limits_produce_no_clause(self, book):
        row = {"req_id": "X", "title": "温度系数", "min": None, "max": None, "unit": "%/℃"}
        asm = assemble(row, role="output_spec", limits_to="output", book=book)
        # 无可测判据 -> 不臆造数值子句
        assert not any(o.source == "limits" for o in asm.outputs)
        assert "no_output_condition" in asm.flags

    def test_kinds_closed_vocabulary(self, book):
        assert book.kinds, "词表为空"
        for r in book.rules:
            assert r.kind in book.kinds, f"{r.id}.kind={r.kind} 不在词表"

    def test_unknown_unit_flagged_not_guessed(self, book):
        row = {"req_id": "X", "title": "某指标", "min": 1.0, "unit": "个怪单位"}
        asm = assemble(row, role="output_spec", limits_to="output", book=book)
        assert "limit_kind_unmapped" in asm.flags


# ---------------- 表结构识别 (T1/T2) ----------------
class TestSchema:
    def test_param_table_matched(self, blocks):
        reg = load_registry()
        b = next(b for b in blocks if b.section_path == "4.3.1")
        det = reg.detect(b.tables[0][0], b.tables[0][1:])
        assert det.matched
        assert det.schema.entity == "requirement"

    def test_multiline_rows_not_merged(self, blocks):
        """同一 req_id 的多档位行必须各自成实体 (曾因 eid 冲突被静默合并)."""
        ents = extract_from_blocks(blocks, "TST", "A")
        eff = [e for e in ents if e.props.get("req_id") == "SR-X-1210"]
        assert len(eff) == 2
        assert len({e.eid for e in eff}) == 2

    def test_infer_roles_recognises_id_and_numeric(self):
        header = ["编号", "数值", "说明"]
        rows = [["A-1", "10", "x"], ["A-2", "20", "y"], ["A-3", "30", "z"]]
        roles = {r.header: r.role for r in infer_roles(header, rows)}
        assert roles["编号"] == "id"
        assert roles["数值"] == "numeric"

    def test_unmapped_table_makes_no_entities(self, blocks):
        """未映射的表不产生实体, 但行仍在 blocks 里 (无损底座)."""
        ents = extract_from_blocks(blocks, "TST", "A")
        assert all(e.etype != "Signal" or "P1" not in str(e.props.get("pin", "")) for e in ents)


# ---------------- 注记机制 ----------------
class TestAnnotations:
    def test_fingerprint_stable(self):
        a = {"req_id": "X", "title": "t", "notes": "n"}
        b = {"req_id": "X", "title": "t", "notes": "n"}
        assert row_fingerprint(a) == row_fingerprint(b)

    def test_fingerprint_changes_with_content(self):
        a = {"req_id": "X", "title": "t", "notes": "n1"}
        b = {"req_id": "X", "title": "t", "notes": "n2"}
        assert row_fingerprint(a) != row_fingerprint(b)


# ---------------- 档案 ----------------
class TestProfiles:
    def test_repo_profiles_load(self):
        book = ProfileBook.load()
        assert book.default_profile in book.profiles

    def test_prior_longest_prefix_wins(self, profile):
        pr = profile.prior_for("4.3.1")
        assert pr.role == "input_domain"
        assert profile.prior_for("4.9").role == profile.default_role

    def test_limits_to_validated(self):
        book = ProfileBook.load()
        for p in book.profiles.values():
            for pr in p.section_priors.values():
                assert pr.limits_to in {"input", "output", "both"}


# ---------------- 端到端 (blocks 通道) ----------------
class TestEndToEnd:
    def test_missing_model_fail_closed(self):
        from aterag.extract.api import extract_test_conditions

        with pytest.raises(ModelNotIngested):
            extract_test_conditions("不存在的型号XYZ")

    def test_full_pipeline(self, profile):
        from aterag.extract.models import ExtractionResult

        blocks = parse_markdown(SAMPLE)
        sel = select_sections(blocks, profile.section_keywords)
        from aterag.extract.api import rows_from_blocks

        rows, review, _ = rows_from_blocks(blocks, "TST", "A", sel.section_prefixes)
        out = apply_sieve(rows, profile.exclude_words)
        kept = {r["req_id"] for r in out.kept}
        assert "SR-X-1101" not in kept  # 等级不要求
        assert "SR-X-1220" not in kept  # 等级不要求
        assert "SR-X-1200" in kept
        assert len(out.kept) + len(out.excluded) == len(rows)
        assert isinstance(ExtractionResult, type)
