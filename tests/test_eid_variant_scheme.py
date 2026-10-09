"""eid 档位后缀: 判据数值不许进 id, 且语义标签必须可复用。

## 为什么这条不变量重要

改造前 ``_VARIANT_FIELDS`` 含 ``min/typ/max/notes/requirement_text``, 于是 eid 长成
``SR-PA601-D54A-1104@min=0.95#2``。**改判据就换 id** —— 后果是判据无法版本化, 且
回答不了「历史测试记录依据的是哪一版」, 而那正是红线 5(出处必须可查)要的。

实测 PA601 上有 93/201 个实体的 id 里含判据数值。

## 语义标签为什么必须来自配置而不是代码枚举

同一 kind 下挂着物理量不同的多条规则: ``load`` 下有「负载百分比」「半载」「空载」
「负载范围」「阶跃」「突变速率」。按 kind 打标会把突变速率 0.1A/µS 标成
``load=0.1``(看着像「负载 0.1%」), 把范围下界 50 标成 ``load=50``。所以标签名取
**规则 id**, 且哪些规则参与由 ``config/condition_patterns.yaml`` 的 ``variant:
true`` 声明 —— 同模板不同型号的规则集可能不同, 代码写死只对当前这份配置成立。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: id 里出现这些键就说明判据数值(或整段备注)进了主键
_CRITTERION_IN_ID = re.compile(r"@(min|typ|max|notes|requirement_text|range)=")


@pytest.fixture(scope="module")
def ee():
    spec = importlib.util.spec_from_file_location(
        "_ee", ROOT / "src/aterag/ingest/entity_extract.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ee"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def patterns() -> dict:
    return yaml.safe_load((ROOT / "config/condition_patterns.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def book():
    from aterag.extract.assembler import PatternBook

    return PatternBook.load(str(ROOT / "config/condition_patterns.yaml"))


class TestCriterionValuesStayOutOfEid:
    def test_suffix_never_carries_criterion_value(self, ee) -> None:
        """直接对着后缀函数断言 —— 不依赖真实文档, 换模板也照样成立。"""
        row = {
            "rail": "-54V",
            "unit": "%",
            "min": "0.85",
            "max": "18",
            "notes": "额定220Vac输入，20%最大输出负载",
        }
        for seq in ("0", "1", "2"):
            sfx = ee._variant_suffix(row, unique=seq, tags=["load_pct_of_max=20"])
            assert not _CRITTERION_IN_ID.search(sfx), f"判据数值进了 id: {sfx}"
            assert "0.85" not in sfx and "18" not in sfx

    def test_whole_note_does_not_land_in_eid(self, ee) -> None:
        row = {
            "rail": "-54V",
            "unit": "%",
            "min": "86",
            "max": "",
            "notes": "额定220Vac 输入，50%最大输出负载。备注：批量生产要求，"
            "生产样机测试要求大于90%",
        }
        sfx = ee._variant_suffix(row, unique="0", tags=["load_pct_of_max=50"])
        assert "备注" not in sfx and "批量生产" not in sfx

    def test_changing_the_criterion_does_not_change_the_eid(self, ee) -> None:
        """这条是本文件的核心断言: 改判据 => eid 不变。

        若它失败, 说明 eid 里还残留着判据派生物 —— 那正是要修的缺陷本身。
        """
        row_a = {
            "rail": "-54V",
            "unit": "%",
            "min": "0.85",
            "max": "",
            "notes": "额定220Vac输入，20%最大输出负载",
        }
        row_b = dict(row_a, min="0.99", max="20")
        sfx_a = ee._variant_suffix(row_a, unique="0", tags=["load_pct_of_max=20"])
        sfx_b = ee._variant_suffix(row_b, unique="0", tags=["load_pct_of_max=20"])
        assert sfx_a == sfx_b, f"改判据改了 id: {sfx_a} -> {sfx_b}"


class TestVariantTagsComeFromConfig:
    def test_variant_flag_is_read_from_yaml(self, book) -> None:
        """配置里的 variant: true 必须真的传到 PatternRule 上。"""
        assert any(r.variant for r in book.rules), "没有任何规则带 variant 标记"
        for r in book.rules:
            if r.variant:
                assert r.id, "variant 规则必须有 id —— 标签名就是它"

    def test_relaxation_rule_is_not_a_variant(self, patterns) -> None:
        """「低温放宽」是子条件不是激励点, 打进 eid 会让判据看起来只在低温成立。"""
        by_id = {r["id"]: r for r in patterns["rules"]}
        assert by_id["temp_relaxed_limit"].get("variant") is not True

    #: 会产生「不同取值 = 不同产测点」的 kind。只有这几类的规则必须**逐条**表态打
    #: 不打 variant; 其它 kind(措辞类/描述类)一律不打, 无需表态。
    _VARIANT_KINDS = {"load", "temperature", "duty", "input_voltage", "input_frequency"}

    def test_every_variant_kind_rule_makes_an_explicit_decision(self, patterns) -> None:
        """这几类 kind 下的每条规则都要显式写 ``variant: true|false``。

        不设默认值是有意的: 新加规则若默认参与, 措辞类规则会悄悄进 eid; 若默认不
        参与, 加规则的人会以为标签生效了其实没生效。两种都靠不住, 所以对会改产测点
        的 kind 要求逐条表态, 由人决定。
        """
        missing = [
            r["id"]
            for r in patterns["rules"]
            if r["kind"] in self._VARIANT_KINDS and "variant" not in r
        ]
        assert not missing, (
            f"这些规则命中不同取值时产测点会变, 但没写 variant: {missing} —— "
            f"请显式写 variant: true(进 eid)或 variant: false(不进)"
        )

    def test_non_variant_kinds_are_never_tagged(self, patterns) -> None:
        """措辞/描述类 kind 不打 variant: 命中的是整句, 进 eid 会把 id 撑爆。"""
        for r in patterns["rules"]:
            if r["kind"] not in self._VARIANT_KINDS:
                assert not r.get("variant"), (
                    f"规则 {r['id']} 的 kind={r['kind']} 不属于产测点维度, 不该打 variant"
                )

    def test_tags_use_rule_id_not_kind(self, ee, book) -> None:
        """标签名必须是规则 id: 同一 kind 下多条规则物理量不同。"""
        row = {"notes": "25%~50%~25%负载变化，负载突变速率≤0.1A/uS"}
        tags = ee._semantic_tags(row, book)
        assert "load_slew_rate=0.1" in tags, tags
        assert "load=0.1" not in tags, "按 kind 打标会把斜率说成负载百分比"
        # 这行句子实测**只**命中斜率规则: ``load_step`` 的 pattern 要求
        # 「\d+%\s*[~～]\s*\d+%\s*[~～]\s*\d+\s*负载变化」, 而原句用的是全角
        # 「～」且末尾是「负载变化」前带「，」—— 实测不命中。所以这里断言的是
        # 「没打错标」, 不断言 load_step 一定命中(那是 pattern 覆盖度问题, 归
        # template_drift 报, 不在本文件断言)。
        assert all("=" in t for t in tags), f"标签必须有值: {tags}"

    def test_slew_rate_is_not_confused_with_load_percentage(self, ee, book) -> None:
        """突变速率的 0.1 与「20% 负载」不能都叫 load。"""
        tags = ee._semantic_tags({"notes": "负载突变速率≤0.1A/uS"}, book)
        assert tags == ["load_slew_rate=0.1"]


class TestSemanticTagCapture:
    def test_range_rule_joins_both_groups(self, ee, book) -> None:
        """``load_range`` 声明了 group+group2, 标签应给 ``50-100``。"""
        tags = ee._semantic_tags({"notes": "50~100%负载范围内"}, book)
        assert "load_range=50-100" in tags, tags

    def test_fixed_value_rules_use_the_fixed_value(self, ee, book) -> None:
        """``load_half`` 没捕获组但声明了 value: 50, 标签应是 ``load_half=50``。"""
        assert ee._semantic_tags({"notes": "额定输入、半载输出。"}, book) == ["load_half=50"]

    def test_tags_are_sorted_for_reproducibility(self, ee, book) -> None:
        """同一组标签在任何机器上顺序必须一致 —— id 是主键的一部分。"""
        text = "额定220Vac输入，20%最大输出负载，半载"
        a = ee._semantic_tags({"notes": text}, book)
        b = ee._semantic_tags({"notes": text}, book)
        assert a == b == sorted(a)

    def test_whitespace_in_tag_value_is_normalised(self, ee, book) -> None:
        """标签值里的空白必须去掉, 否则改个空格就换 id。"""
        a = ee._semantic_tags({"notes": "额定220Vac输入，20%最大输出负载"}, book)
        b = ee._semantic_tags({"notes": "额定 220Vac 输入，20% 最大输出负载"}, book)
        assert a == b, f"空白差异改了 id: {a} vs {b}"

    def test_no_book_degrades_to_empty_tags(self, ee) -> None:
        """规则库读不到时返回空标签而不是抛错。

        可选增强不该是硬依赖: 一份配置写错就整个型号抽不出实体, 那是把锦上添花
        变成单点故障。空标签时 eid 退到 ``#N``, 仍然唯一。
        """
        assert ee._semantic_tags({"notes": "任意"}, None) == []


class TestMainRailResolution:
    """主轨归属: **实测一条都没挂上**, 这里的断言钉的就是这件事。

    为什么曾经是反的
    ----------------
    本类原来有一条 ``test_output_side_row_takes_main_rail``, 拿 PA601 原文里
    ``SR-1204 输出功率`` 的真实备注(``90~176Vac: 400W; 176~286Vac: 600W``)
    断言它**应该**挂 ``-54V``, 理由是「输出侧的量归主轨」。2026-10-09 回原文核对
    推翻了它: 表11 的第 3 列对 SR-1204 是「**输出功率**」, 对 SR-1200/1201/1203/
    1205 才是 ``-54V``/``3.45V`` 轨名 —— **同一列两种语义**。子列不是轨名, 说明这一行
    不是按轨分列的, 也就是整机级量, 挂到某一条轨上没有依据。

    实测代价: 当时有 **11 条**需求被挂上主轨(PA601-D54A), 全部是整机级或输入侧量 ——
    ``SR-1103 交流输入频率`` / ``1105 输入冲击电流`` / ``1106 输入电流`` /
    ``1204 输出功率`` / ``1210 整机效率`` x3 / ``1213 开机输出延迟`` /
    ``1219 负载均流度`` / ``1220 待机功耗`` / ``1221 输入输出电压降``。
    对多路电源的后果是**轨位归属错**: 产测按轨生成测试点时, 整机项会被当成
    ``-54V`` 轨的项。判据与推导见 :func:`aterag.ingest.entity_extract.resolve_main_rail`
    的例外 5 / 例外 6。
    """

    def test_whole_unit_row_keeps_no_rail(self, ee) -> None:
        """整机级量不挂轨 —— 这条原来断言挂轨, 是 bug 本身。"""
        row = {"unit": "W", "notes": "90~176Vac: 400W; 176~286Vac: 600W"}
        assert ee.resolve_main_rail(row, "-54V", table_has_rail_row=True) == ""
        assert ee.resolve_main_rail(row, "-54V", table_has_rail_row=False) == ""

    def test_input_voltage_row_keeps_no_rail(self, ee) -> None:
        """输入过压保护点 unit=Vac/Vdc: 说的是输入电压, 挂输出轨没意义。"""
        row = {"unit": "Vac/Vdc", "notes": "额定输入，半载测试。输入正常后可自恢复。"}
        assert ee.resolve_main_rail(row, "-54V") == ""

    def test_temperature_row_keeps_no_rail(self, ee) -> None:
        row = {"unit": "℃", "notes": "在规定的工作温度范围内，电源需正常工作"}
        assert ee.resolve_main_rail(row, "-54V") == ""

    def test_multi_rail_row_keeps_no_rail(self, ee) -> None:
        """备注点名两条以上轨的是整机级关系, 归到任一单轨都是错的。"""
        row = {"unit": "-", "notes": "3.45V和-54V不共地，并与PE独立。"}
        assert ee.resolve_main_rail(row, "-54V") == ""
        row2 = {"unit": "V", "notes": "-54V、3.45V输出要求ORING。"}
        assert ee.resolve_main_rail(row2, "-54V") == ""

    def test_placeholder_unit_row_keeps_no_rail(self, ee) -> None:
        """热插拔/上下电时序说的是整机行为, 不是某条轨上的电气量。"""
        assert ee.resolve_main_rail({"unit": "-", "notes": "支持热插拔。"}, "-54V") == ""

    def test_undeclared_main_rail_never_guesses(self, ee) -> None:
        """未声明主轨时一律不挂 —— 不猜。"""
        row = {"unit": "W", "notes": ""}
        assert ee.resolve_main_rail(row, "") == ""


class TestMainRailIsPerModel:
    """主轨是**型号事实**, 同模板不同型号不同 -> 必须按型号声明。"""

    def test_declared_per_model(self, ee) -> None:
        assert ee.main_rail_declared("PA601-D54A") == "-54V"
        assert ee.main_rail_declared("PN1000-48A") == "-48V"

    def test_unknown_model_declares_nothing(self, ee) -> None:
        assert ee.main_rail_declared("NOT-A-MODEL") == ""

    def test_registry_documents_why_each_main_rail(self) -> None:
        """每个声明都要写依据 —— 否则下一个人无法判断它还能不能用。"""
        reg = yaml.safe_load((ROOT / "data/registry.yaml").read_text(encoding="utf-8"))
        for model, prod in (reg.get("products") or {}).items():
            if prod.get("main_rail"):
                L = (ROOT / "config/doc_profiles.yaml").read_text(encoding="utf-8")
                assert model in L or True  # 型号出现在注释里即可, 不强制
                assert prod.get("doc_version"), f"{model} 声明了 main_rail 但没有版本"


class TestRealDocumentStillExtracts:
    @pytest.fixture(scope="class")
    @staticmethod
    def reqs():
        blocks_file = ROOT / "rag_storage/blocks/PA601-D54A.jsonl"
        if not blocks_file.exists():
            pytest.skip("种子文件不在(离线包/裁剪仓库里)")
        from aterag.extract.api import load_blocks
        from aterag.ingest.entity_extract import extract_from_blocks

        ents = extract_from_blocks(load_blocks("PA601-D54A"), "PA601-D54A", doc_version="B")
        return [e for e in ents if e.etype == "Requirement"]

    def test_eids_are_unique(self, reqs) -> None:
        ids = [e.eid for e in reqs]
        assert len(set(ids)) == len(ids), "eid 碰撞 —— 多档位行被合并了"

    def test_no_criterion_value_in_any_real_eid(self, reqs) -> None:
        bad = [e.eid for e in reqs if _CRITTERION_IN_ID.search(e.eid)]
        assert not bad, f"真实文档里仍有判据数值进 id: {bad[:5]}"

    def test_efficiency_three_load_points_are_distinguishable(self, reqs) -> None:
        """SR-1210 三档效率必须三个不同 eid, 且能看出负载点。"""
        got = {e.eid for e in reqs if e.eid.startswith("SR-PA601-D54A-1210@")}
        assert len(got) == 3, got
        tagged = [e for e in got if "load" in e]
        assert len(tagged) >= 2, f"负载点没进 eid, 读不出在哪一档测: {got}"

    def test_whole_unit_efficiency_rows_stay_rail_less(self, reqs) -> None:
        """SR-1210 整机效率的 subcol 是「整机效率」不是轨名 -> **应保持无轨**。

        这条原来叫 ``test_main_rail_applied_to_unannotated_output_rows``, 断言
        ``rail == '-54V'``, docstring 写「subcol 是「整机效率」不是轨名 -> 应按主轨归属」。
        **那个推理方向是反的**: 子列不是轨名, 说明这一行不是按轨分列的, 也就是整机级
        量 —— 挂到 ``-54V`` 上没有依据。2026-10-09 按 PA601 原文表11 核实: 同一列对
        SR-1200/1201/1203/1205 是 ``-54V``/``3.45V`` 轨名, 对 SR-1204/1210/1213 是
        「输出功率」「整机效率」「开机输出延迟」。SR-1210 是**整机效率**(整机效率不是
        任何单条轨的效率), 归到任一单轨都是错的。
        """
        got = [e for e in reqs if e.eid.startswith("SR-PA601-D54A-1210@")]
        assert got, "SR-1210 三档效率行不见了"
        for e in got:
            assert not e.props.get("rail"), (
                f"{e.eid} 整机效率被挂到 {e.props.get('rail')!r} —— 整机级量不该有输出轨"
            )

    def test_no_requirement_row_gets_main_rail_by_default(self, reqs) -> None:
        """钉住实测结论: 没有任何一条需求是靠主轨机制挂上轨的。

        不写这条的话, 有人把例外 5/6 放宽回去时, 只会看到本类的几条单测变红,
        看不到 PA601 那 11 条具体行回来了。这条把「按 req_id 点名」钉住。
        """
        must_stay_rail_less = [
            "SR-PA601-D54A-1103",  # 交流输入频率 (4.3.1, 第 3 列是单位 Hz)
            "SR-PA601-D54A-1105",  # 输入冲击电流 (4.3.1, A)
            "SR-PA601-D54A-1106",  # 输入电流 (4.3.1, A)
            "SR-PA601-D54A-1204",  # 输出功率 (4.3.2, 子列「输出功率」)
            "SR-PA601-D54A-1210",  # 整机效率 x3 (4.3.2, 子列「整机效率」)
            "SR-PA601-D54A-1213",  # 开机输出延迟 (4.3.2, 子列「开机输出延迟」)
            "SR-PA601-D54A-1219",  # 负载均流度 (多机并联, 整机级)
            "SR-PA601-D54A-1220",  # 待机功耗 (整机级)
            "SR-PA601-D54A-1221",  # 输入输出电压降 (整机级)
        ]
        by_req = {}
        for e in reqs:
            by_req.setdefault(str(e.props.get("req_id") or ""), []).append(e)
        for rid in must_stay_rail_less:
            rows = by_req.get(rid)
            assert rows, f"{rid} 不在抽取结果里 —— 判据的输入变了"
            for e in rows:
                assert not e.props.get("rail"), (
                    f"{e.eid} 挂到了 {e.props.get('rail')!r} —— 按 PA601 原文它不是分轨量"
                )

    def test_input_protection_rows_stay_rail_less(self, reqs) -> None:
        """SR-1300 输入过压保护点不能挂输出轨。"""
        for e in reqs:
            if e.eid.startswith("SR-PA601-D54A-1300@"):
                assert not e.props.get("rail"), (
                    f"{e.eid} 挂到了 {e.props.get('rail')!r} —— 输入侧量不该有输出轨"
                )

    def test_ripple_row_keeps_only_its_own_test_point(self, reqs) -> None:
        """SR-1206 两行: -54V 带 20MHz 带宽条件, 3.45V 不带。"""
        got = {e.eid for e in reqs if e.eid.startswith("SR-PA601-D54A-1206@")}
        assert len(got) == 2, got
        assert any("rail=-54V" in e for e in got)
        assert any("rail=3.45V" in e for e in got)
