"""多型号需求提取的回归测试。

背景: 落库侧 ``_signal_type`` 改成**依赖章节角色**(见
``persist_requirements._signal_type``)之后, 「档案与型号的章节号对不上」就成了
一个会静默出坏结果的故障: ``DocProfile.prior_for`` 未命中时回落
``default_role``(stimulus_response), 于是该型号的**全部判据被判成 TEST** ——
信号项变成电气项, 而 stats 里看不出任何异常。

本章因此钉住一条不变式: **每个已入库的注册型号, ``stats.prior_fallback`` 必须为
0**。这条不成立就是「档案配错了型号」, 而它以前是查不出来的。

另有一条更本质的: 已入库型号必须能跑通抽取 —— 否则「支持其他型号」只是档案里
多写了几段 YAML。
"""

from __future__ import annotations

import pytest

from aterag.config import get_settings
from aterag.extract import ProfileBook, extract_test_conditions, load_annotations
from aterag.extract.models import ModelNotIngested
from aterag.ingest.persist_requirements import ROLE_SIGNAL_IO, rows_from_result
from aterag.registry import Registry

REGISTRY = Registry.load(get_settings())
PROFILES = ProfileBook.load()


def _ingested(model_id: str) -> bool:
    return (Path_blocks(model_id)).exists()


from pathlib import Path  # noqa: E402


def Path_blocks(model_id: str) -> Path:
    return Path("rag_storage/blocks") / f"{model_id}.jsonl"


INGESTED = [m for m in sorted(REGISTRY.products) if _ingested(m)]
NOT_INGESTED = [m for m in sorted(REGISTRY.products) if not _ingested(m)]


def _extract(model_id: str):
    entry = REGISTRY.products[model_id]
    return extract_test_conditions(
        model_id, doc_version=entry.doc_version, annotations=load_annotations(model_id)
    )


class TestPriorMatching:
    """前缀匹配的边界 —— 这里写死具体章节号是有意的: 它们就是档案里配的键。"""

    @pytest.mark.parametrize("sec", ["4.3.4.1", "4.3.4.2", "4.3.4.3", "4.3.4.5"])
    def test_child_sections_hit_their_parent_prior(self, sec):
        """子节必须命中父级先验 —— 档案配 ``4.3.4`` 就要管住 ``4.3.4.1``。"""
        prof = PROFILES.profiles[PROFILES.default_profile]
        assert prof.matched_prior(sec) is not None, f"{sec} 未命中, 会静默回落"

    @pytest.mark.parametrize("sec", ["4.3", "4.4", "5", ""])
    def test_unconfigured_sections_are_reported_as_fallback(self, sec):
        """未配的章节必须能被 ``matched_prior`` **看出来**, 而不是只有 prior_for 静默兜底。

        这条是整个多型号安全网的基础: 看不出来的回落就等于没有回落保护。
        """
        prof = PROFILES.profiles[PROFILES.default_profile]
        assert prof.matched_prior(sec) is None
        # prior_for 仍然给出可用的兜底(抽取不该因缺档案而崩), 但信息在 matched_prior 上
        assert prof.prior_for(sec).role == prof.default_role

    def test_prior_for_and_matched_prior_agree_when_hit(self):
        """命中时两者必须同源 —— 否则 role 与统计口径会分叉。"""
        prof = PROFILES.profiles[PROFILES.default_profile]
        for sec in ("4.3.1", "4.3.2", "4.3.3", "4.3.5"):
            key, prior = prof.matched_prior(sec)
            assert prof.prior_for(sec) is prior


class TestEveryIngestedModelExtracts:
    def test_at_least_one_model_is_ingested(self):
        """本测试集若一个型号都没入库, 下面全部会 vacuous 地通过。"""
        assert INGESTED, "没有已入库型号 —— 多型号测试形同虚设"

    @pytest.mark.parametrize("model_id", INGESTED)
    def test_extraction_succeeds(self, model_id):
        result = _extract(model_id)
        assert result.conditions, f"{model_id} 抽出 0 条条件"

    @pytest.mark.parametrize("model_id", INGESTED)
    def test_prior_fallback_is_zero(self, model_id):
        """**核心不变式**: 档案章节号必须与该型号规格书对得上。

        非 0 意味着有行回落到了 ``default_role`` —— 落库侧会因此把信号判据
        当成电气判据(见 ``persist_requirements._signal_type`` 的 docstring)。
        """
        result = _extract(model_id)
        prof = REGISTRY.products[model_id].doc_profile or PROFILES.default_profile
        assert result.stats.get("prior_fallback", -1) == 0, (
            f"{model_id} 用档案 {prof}(先验键 "
            f"{sorted(PROFILES.profiles[prof].section_priors)}), 但有 "
            f"{result.stats['prior_fallback']} 行的 section_path 没命中 —— "
            "章节号对不上, 该型号的信号判据会被误判成电气判据"
        )

    @pytest.mark.parametrize("model_id", INGESTED)
    def test_roles_are_not_all_default(self, model_id):
        """不能所有判据都拿到默认 role —— 那是「档案完全没生效」的另一种表现。"""
        result = _extract(model_id)
        roles = {c.role for c in result.conditions}
        assert roles - {
            PROFILES.profiles[
                REGISTRY.products[model_id].doc_profile or PROFILES.default_profile
            ].default_role
        }, f"{model_id} 的 role 全是默认值, 档案没起作用"


class TestSignalTypeFollowsRole:
    """``_signal_type`` 依赖 role, 所以 role 判据错了这里就跟着错。"""

    @pytest.mark.parametrize("model_id", INGESTED)
    def test_signal_io_role_never_becomes_test(self, model_id):
        """``signal_io`` 角色的判据不得被归成 TEST。

        被归成 TEST 意味着工装去绑探头而不是通信/干接点通道 —— 少绑通信通道时
        遥测项根本读不到数, 且不报错。
        """
        result = _extract(model_id)
        rows = rows_from_result(result)
        by_sr = {}
        for r in rows:
            by_sr.setdefault(r.sr_id, []).append(r)
        signal_srs = {c.req_id for c in result.conditions if c.role == ROLE_SIGNAL_IO}
        for sr_id in signal_srs:
            got = {r.signal_type for r in by_sr.get(sr_id, [])}
            assert "TEST" not in got, f"{sr_id} 是 signal_io 角色却被归成 TEST"

    @pytest.mark.parametrize("model_id", INGESTED)
    def test_electrical_roles_are_not_signal_typed(self, model_id):
        """反过来: 非信号角色的判据不得被判成 YX/YC/PROT。

        标成信号类会让工装去绑干接点/通信通道, 而不是电压电流探头。
        """
        result = _extract(model_id)
        rows = rows_from_result(result)
        by_sr = {}
        for r in rows:
            by_sr.setdefault(r.sr_id, []).append(r)
        for c in result.conditions:
            if c.role == ROLE_SIGNAL_IO or c.role == "protection_response":
                continue
            got = {r.signal_type for r in by_sr.get(c.req_id, [])}
            assert not (got & {"YX", "YC", "PROT"}), f"{c.req_id}(role={c.role}) 被判成信号类 {got}"


class TestMissingModelFailsLoudly:
    def test_not_ingested_model_raises_named_error(self):
        """未入库的型号必须报出**指名**的错误, 而不是抽出 0 条或回退到别的型号。

        实测 PN2000-24A 属此类: 档案已写好(power_spec_cn_pn2000)但没有 blocks,
        抽取报 ``未找到 ... 尚未入库``。这条测试保证该错误不会被静默吞掉。
        """
        if not NOT_INGESTED:
            pytest.skip("全部型号都已入库")
        with pytest.raises(ModelNotIngested):
            _extract(NOT_INGESTED[0])


class TestProfilePerModel:
    def test_declared_profile_exists(self):
        """``registry.doc_profile`` 指向的档案必须存在 —— 指向不存在的档案等于
        悄悄用默认档案, 而默认档案的章节号几乎必然对不上。"""
        for model_id, entry in REGISTRY.products.items():
            if entry.doc_profile:
                assert entry.doc_profile in PROFILES.profiles, (
                    f"{model_id} 声明档案 {entry.doc_profile} 但 doc_profiles.yaml 里没有"
                )

    def test_distinct_profiles_have_distinct_section_keys(self):
        """不同档案的先验键不应重叠 —— 重叠说明两份档案其实是一份, 那第二份是死配置。"""
        seen: dict[tuple[str, ...], str] = {}
        for name, prof in PROFILES.profiles.items():
            key = tuple(sorted(prof.section_priors))
            assert key not in seen, f"档案 {name} 与 {seen.get(key)} 的先验键完全相同"
            seen[key] = name
