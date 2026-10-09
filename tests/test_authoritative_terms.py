"""权威术语补全的不变量 (websearch 查证 2026-10-07 引入)。

## 背景

术语表里「额定」与「标称」只在**具体量纲**上出现, 没有抽象条目, 于是每个量纲
各自决定要不要区分 —— 结果是: 有出处的部分(容量那对)区分了, 没出处的部分
(电压那对)互为同义词。本模块补上抽象条目, 并把互为同词的别名摘掉。

## 三条踩过的坑, 各有一个测试

1. **改名与摘除的顺序**: ``CONFLATED_SYNONYMS`` 按新 id(``VOUT_RATED``)书写,
   而 ``apply_concept_fixes`` 先跑才轮到 ``strip_conflated_synonyms``。顺序反过来
   时, 摘除按旧 id 查不到, **静默什么都不摘** —— 生成日志里也只有一行, 不核对
   种子就会以为成功了。:class:`TestStripActuallyStrips` 用「每一条都必须真被摘掉」
   堵住它。
2. **查证深度有两种**: 条款号出现在标准的术语清单里 ≠ 定义正文被逐字抄下来。
   混为一谈会让读者以为每条都核对过原文。:class:`TestVerbatimIsHonest` 钉住标记。
3. **引用粒度**: 只给标准号不给条款号, 读者仍要自己翻几百页找哪一条 ——
   而「哪一条」正是本条与相邻术语的全部区别所在。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SEED_JSON = ROOT / "data/seed/power_domain_seed.json"
BUILDER = ROOT / "scripts/build_seed_data.py"


def _builder_module():
    """把 build_seed_data 当模块导入 —— 常量是单一事实源, 不在测试里复述一遍。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_build_seed_data", BUILDER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bs():
    return _builder_module()


@pytest.fixture(scope="module")
def seed_by_id() -> dict[str, dict]:
    import json

    doc = json.loads(SEED_JSON.read_text(encoding="utf-8"))
    return {r["id"]: r for r in doc["records"] if isinstance(r, dict) and "id" in r}


class TestAuthoritativeTerms:
    def test_rated_and_nominal_are_separate_terms(self, bs, seed_by_id) -> None:
        """抽象层的区分是这一整批的目的, 少一个就回到「各自决定」。"""
        for cid in ("RATED_VALUE", "NOMINAL_VALUE"):
            assert cid in seed_by_id, f"抽象术语 {cid} 没进种子"

    def test_they_are_not_synonyms_of_each_other(self, bs, seed_by_id) -> None:
        """互为同义词就是这一批要修的问题本身。"""
        rated = set(seed_by_id["RATED_VALUE"].get("synonyms") or [])
        nominal = set(seed_by_id["NOMINAL_VALUE"].get("synonyms") or [])
        assert rated & nominal == set(), f"额定/标称仍互为同义词: {rated & nominal}"

    def test_both_cite_the_distinguishing_clauses(self, bs, seed_by_id) -> None:
        """442-01-01 与 442-01-04 之间的区别是这批术语存在的全部理由。"""
        assert seed_by_id["RATED_VALUE"]["standard_section"] == "442-01-01"
        assert seed_by_id["NOMINAL_VALUE"]["standard_section"] == "442-01-04"

    def test_clause_number_is_cited_not_just_the_standard(self, bs, seed_by_id) -> None:
        """只给标准号的话, 「哪一条才是区别所在」还是没回答。"""
        clause = re.compile(r"^\d+(-\d+)+$")
        for spec in bs.AUTHORITATIVE_TERMS:
            sec = spec["standard_section"]
            assert clause.match(str(sec)), (
                f"{spec['id']} 的 standard_section 看着不像条款号: {sec!r}"
            )
            assert seed_by_id[spec["id"]]["authority_ref"].endswith(sec), (
                f"{spec['id']} 的 authority_ref 没落到条款号"
            )

    def test_standard_authority_requires_a_reference(self, bs, seed_by_id) -> None:
        """门禁已经要求 standard 级给 authority_ref; 这里确认这批没被降级绕过。"""
        for spec in bs.AUTHORITATIVE_TERMS:
            assert seed_by_id[spec["id"]]["authority_kind"] == "standard", (
                f"{spec['id']} 不该是 {seed_by_id[spec['id']]['authority_kind']}"
            )


class TestVerbatimIsHonest:
    def test_non_verbatim_definitions_are_marked_in_the_seed(self, bs, seed_by_id) -> None:
        """没逐字核对过的释义, 必须在**落库文本里**留痕, 不只在源码注释里。"""
        for spec in bs.AUTHORITATIVE_TERMS:
            rec = seed_by_id[spec["id"]]
            assert "definition_verbatim" in rec, f"{spec['id']} 没记逐字与否"
            assert rec["definition_verbatim"] == spec.get("verbatim", True)

    def test_non_verbatim_definitions_say_so_inline(self, bs, seed_by_id) -> None:
        """只在 properties 里存一个布尔值不够 —— 读定义正文的人看不到那个标志。"""
        for spec in bs.AUTHORITATIVE_TERMS:
            if spec.get("verbatim", True):
                continue
            d = str(seed_by_id[spec["id"]]["definition"])
            assert "未逐字核对" in d, (
                f"{spec['id']} 释义是本项目撰写的, 但正文里没标 —— 读定义的人会以为核对过原文"
            )

    def test_verbatim_and_non_verbatim_both_exist(self, bs) -> None:
        """两种深度都存在才说明标志有区分力; 全 True 等于没做区分。"""
        flags = {spec.get("verbatim", True) for spec in bs.AUTHORITATIVE_TERMS}
        assert flags == {True, False}, "verbatim 标志没有区分开两类查证深度"


class TestStripActuallyStrips:
    def test_every_conflated_alias_was_really_removed(self, bs, seed_by_id) -> None:
        """本轮实际踩过: 改名先跑、摘除按旧 id 查不到, **静默什么都不摘**。

        所以这里不看日志、不看返回值, 直接查种子: 每条清单里的别名都必须
        真的不在同义词里。
        """
        for cid, alias, _basis in bs.CONFLATED_SYNONYMS:
            syn = seed_by_id[cid].get("synonyms") or []
            assert alias not in syn, f"{cid} 的同义词里还有 {alias!r} —— 清单失效(改名顺序反了?)"

    def test_removed_alias_is_recorded_with_its_basis(self, bs, seed_by_id) -> None:
        """摘掉而不留依据, 审计时无法回答「为什么这个别名没了」。

        ``state`` 区分「本次摘掉」与「上游(corrections.yaml)已摘, 清单项陈旧」
        —— 后者同样要记账: 陈旧清单项静默无输出时, 下一次上游不再摘它就会
        悄悄失效。
        """
        for cid, alias, _basis in bs.CONFLATED_SYNONYMS:
            dropped = seed_by_id[cid].get("dropped_synonyms") or []
            hit = [d for d in dropped if d["alias"] == alias]
            assert len(hit) == 1, (
                f"{cid} 对 {alias!r} 的 dropped_synonyms 记账 {len(hit)} 条, 应恰好 1 条"
            )
            assert hit[0].get("basis"), f"{cid}/{alias} 没记依据"
            assert hit[0].get("state") in {"removed", "already_absent"}, (
                f"{cid}/{alias} 的 state={hit[0].get('state')!r} 不认识"
            )

    def test_no_concept_conflates_rated_with_nominal(self, seed_by_id) -> None:
        """整体自查: 除父概念 CAPACITY(已注记为有意)外, 不该再有概念同时含两词。"""
        offenders = []
        for cid, rec in seed_by_id.items():
            if rec.get("entity_type") != "power_concept" or cid == "CAPACITY":
                continue
            blob = (
                str(rec.get("name", ""))
                + " "
                + " ".join(str(x) for x in (rec.get("synonyms") or []))
            )
            if "额定" in blob and "标称" in blob:
                offenders.append(cid)
        assert not offenders, f"仍把额定/标称混同的概念: {offenders}"


class TestConceptRenames:
    def test_renamed_concept_keeps_a_trail_to_the_old_id(self, bs, seed_by_id) -> None:
        """改名不留旧名就成了无法追溯的静默变更。"""
        for old_id, new_id in bs.CONCEPT_RENAMES.items():
            assert old_id not in seed_by_id, f"{old_id} 应该已经改名走了"
            assert seed_by_id[new_id].get("renamed_from") == old_id

    def test_rename_target_does_not_collide_with_a_kept_concept(self, bs) -> None:
        """新 id 撞上一个已有概念 = 静默覆盖一条知识。"""
        new_ids = set(bs.CONCEPT_RENAMES.values())
        assert len(new_ids) == len(bs.CONCEPT_RENAMES), "CONCEPT_RENAMES 有重复目标"
        assert not (new_ids & set(bs.CONCEPT_RENAMES)), "改名链成环"


class TestAliasPatch:
    def test_parent_concept_notes_that_child_words_are_not_synonyms(self, bs, seed_by_id) -> None:
        """CAPACITY 的别名含「额定容量/标称容量」是有意的 —— 但必须写明为什么。"""
        cap = seed_by_id["CAPACITY"]
        assert {"额定容量", "标称容量"} <= set(cap.get("synonyms") or [])
        assert "不代表它们同义" in str(cap.get("note")), (
            "父概念别名收了子概念的词, 却没写明那不代表同义 —— 读者会当成又一处额定/标称混淆"
        )


class TestDualSettlingTime:
    def test_two_settling_time_entries_are_both_present(self, bs, seed_by_id) -> None:
        """建立时间在标准里是**两个条目**, 报数可能差一整段振荡时间。"""
        assert "SETTLING_TIME" in seed_by_id
        assert "CONTROL_SETTLING_TIME" in seed_by_id
        assert (
            seed_by_id["SETTLING_TIME"]["standard_section"]
            != seed_by_id["CONTROL_SETTLING_TIME"]["standard_section"]
        )

    def test_each_cross_references_the_other(self, bs, seed_by_id) -> None:
        """两条只有互相点名, 读者才知道自己引的是哪一条。"""
        assert "CONTROL_SETTLING_TIME" in str(seed_by_id["SETTLING_TIME"].get("note"))
        assert "SETTLING_TIME" in str(seed_by_id["CONTROL_SETTLING_TIME"].get("note"))


class TestNoDanglingCrossReferenceInNotes:
    def test_notes_only_refer_to_concepts_that_exist(self, bs, seed_by_id) -> None:
        """note 里点名别的概念时, 那个概念得真在种子里。

        本轮踩过: ``RIPPLE`` 的 note 提到 ``NOISE``(§312-07-04), 而 NOISE 那时
        还没加 —— 一条指向不存在概念的注释比没有注释更坏。
        """
        # 全大写标识符在 note 里有好几个**非概念**命名空间, 不排除就是误报:
        # 规则 id(K-*, 在 rules.yaml)、型号(PA601, 在 data/registry.yaml)。
        rules_yaml = ROOT / "domain_rules/power/rules.yaml"
        allowed: set[str] = set()
        if rules_yaml.is_file():
            allowed |= set(
                re.findall(
                    r"^\s*- id:\s*([A-Z0-9_-]+)",
                    rules_yaml.read_text(encoding="utf-8"),
                    re.M,
                )
            )
        registry = ROOT / "data/registry.yaml"
        if registry.is_file():
            reg = registry.read_text(encoding="utf-8")
            # 型号既有 PA601-D54A 这样的, note 里也常只写 PA601 -> 前缀一并收入
            for mid in re.findall(r"\b([A-Z]{2,}[0-9]+(?:-[A-Z0-9]+)?)\b", reg):
                allowed.add(mid)
                allowed.add(mid.split("-")[0])

        offenders = []
        for spec in bs.AUTHORITATIVE_TERMS:
            note = str(seed_by_id[spec["id"]].get("note") or "")
            for token in note.replace("(", " ").replace(")", " ").replace("§", " ").split():
                bare = token.strip("。，,;:：、")
                if not re.match(r"^[A-Z][A-Z0-9_]{3,}$", bare):
                    continue
                if bare in seed_by_id or bare in allowed:
                    continue
                offenders.append((spec["id"], bare))
        assert not offenders, f"note 里点名了不存在的概念: {offenders}"
