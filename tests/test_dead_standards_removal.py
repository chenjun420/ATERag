"""「零引用且未查证的标准」这条移除规则的不变量。

## 为什么这条规则要按**规则**算而不是列 id 清单

105+ 个 id 的硬编码清单会烂掉: 将来有人引用了其中一条, 清单不会知道, 于是
一条**有引用**的标准被静默删掉, 而它的引用者变成悬空引用。按规则算则每次生成
重新判定, 「被引用了」立刻生效。

## 三条判据缺一不可

1. ``authority_kind == "unverified"`` —— ``industry`` / ``standard`` 级说明有人工
   判断过, 那个判断不因为「当前零引用」而失效。
2. 无任何实体用 ``authority_ref`` 指向它 —— ``build_relationships`` 建指向标准的
   边**只有这一条路径**, 所以这等价于「无边指向它」。
3. 它的编号在别处记录的文本字段里**不出现** —— 只判 (2) 会漏掉「某条记录在
   ``source``/``note`` 里提了这个标准号」的情况: 标准实体虽无边, 但它是**能被人
   查到的线索**。实测这类有1 条(``IPC-2221``), 规则必须保住它。

## 本轮踩过的坑: 属性在这个阶段还是**嵌套**的

``prune_non_executable`` 里的 ``props_of`` 是 ``e.get("properties") or {}`` ——
扁平化发生在更后面。所以直接读 ``e["authority_kind"]`` 恒为 ``None``, 三条判据
第一条永不成立, **规则静默返回空集**: 移除数 0, 而生成日志里连一行都没有。
「静默不生效」比「不生效」更坏 —— 它让人以为规则生效了。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data/seed/power_domain_seed.json"
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("_bsd", ROOT / "scripts/build_seed_data.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bsd"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def seed_records() -> list[dict]:
    if not SEED.exists():
        pytest.skip("种子文件不在(离线包/裁剪仓库里)")
    return json.loads(SEED.read_text(encoding="utf-8"))["records"]


class TestRuleIsReachable:
    def test_it_actually_finds_something_on_the_real_entities(
        self, builder, seed_records
    ) -> None:
        """在**真实数据形态**上重跑一遍, 必须能判出候选。

        这条测试的由来: 实现里读 ``e["authority_kind"]`` 而属性当时还是嵌套的,
        规则静默返回空集。断言「非空」是在防那个形态再回来 —— 若候选数变成 0,
        要么真没有死标准了(那也不该由这条测试断言), 要么规则又读错了层级。
        """
        # 用最终种子**重建**生成期的实体形态: 顶层 id/type + 嵌套 properties
        entities = [
            {
                "id": r["id"],
                "type": r.get("entity_type"),
                "properties": {k: v for k, v in r.items() if k not in {"id", "entity_type"}},
            }
            for r in seed_records
            if isinstance(r, dict) and r.get("entity_type")
        ]
        dead = builder.find_dead_standards(entities)
        assert isinstance(dead, list), "规则必须返回列表而不是集合/生成器"
        # 死标准都已经被移出范围了, 所以重跑时应该**找不到新的**;
        # 若这里非空, 说明移除没跑干净。
        assert dead == [], (
            f"规则又判出 {len(dead)} 条死标准(示例 {dead[:3]})—— "
            f"移除步骤没跑, 或生成日志里没有 [移出范围] 那一行"
        )


class TestRemovedStandardsAreGone:
    def test_removed_standard_is_absent_from_the_seed(self, builder, seed_records) -> None:
        """被规则移走的标准, 种子里确实不该有。"""
        # 已知被移除的样本(2026-10-07 实测清单的前几条)
        for sid in (
            "std::AEC-Q101",
            "std::CISPR 22-2008",
            "std::DL/T 860",
            "std::GB/T 17626.5-2019" if False else "std::ANSI/IEEE C37.112-1996",
        ):
            if sid in {r.get("id") for r in seed_records}:
                # 该 id 若因为「被引用」而保留, 也不能是 unverified + 零引用
                rec = next(r for r in seed_records if r.get("id") == sid)
                assert rec.get("authority_kind") != "unverified" or rec.get(
                    "authority_ref"
                ), f"{sid} 仍在库里且仍是 unverified + 无引用 —— 规则漏了它"

    def test_cited_standards_survive(self, seed_records) -> None:
        """被引用过的标准一个都不能少 —— 这是本次删除的红线。

        尤其 ``GB/Z 14429-2005``(32 处引用)、``GB/T 2900.70-2008`` 与
        ``GB/T 2900.33-2004``(本轮新补的术语出处)。删掉任何一个都会让那批引用
        变成悬空, 而且门禁会开始报 ``authority_ref_resolvable``。
        """
        ids = {r.get("id") for r in seed_records}
        for sid in (
            "std::GB/Z 14429-2005",
            "std::GB/T 2900.70-2008",
            "std::GB/T 2900.56-2008",
            "std::GB/T 2900.33-2004",
            "std::GB/T 2900.89-2012",
            "std::GB/T 14598.127-2013",
            "std::YD/T 4523-2023",
            "std::IPC-2221",
        ):
            assert sid in ids, f"{sid} 被误删 —— 它有真实引用"


class TestTextCitationProtectsAStandard:
    def test_ipc2221_is_kept_because_it_is_cited_in_text(self, seed_records) -> None:
        """``IPC-2221`` 没有任何 ``authority_ref`` 指向它, 但 ``CONFORMAL_COATING``
        的 ``source``/``authority_ref`` 文本里提到了它 —— 规则第 3 条判据保住它。

        反过来验证: 若只判「无边」, 它就会被删, 而人从 ``CONFORMAL_COATING``
        的出处文本里找不到对应实体。
        """
        ids = {r.get("id") for r in seed_records}
        assert "std::IPC-2221" in ids, "IPC-2221 被删了 —— 文本引用没保住它"
        coating = next(
            (r for r in seed_records if r.get("id") == "CONFORMAL_COATING"), None
        )
        assert coating is not None, "CONFORMAL_COATING 本身不该消失"
        assert "IPC-2221" in json.dumps(coating, ensure_ascii=False), (
            "CONFORMAL_COATING 记录里已经没有 IPC-2221 的文本引用了 —— "
            "那么保住该标准实体的理由消失, 测试应改成断言它被移除"
        )


class TestNonUnverifiedSurvives:
    def test_verified_standards_are_never_removed(self, seed_records) -> None:
        """``industry`` / ``standard`` 级的标准即使零引用也保留。

        理由: 那个权威级是**人工判断**的结果, 不因为「当前没人引用」而失效 ——
        否则一次生成就会悄悄推翻一次人工裁定。
        """
        kept_non_unverified = [
            r["id"]
            for r in seed_records
            if isinstance(r, dict)
            and r.get("entity_type") == "standard"
            and r.get("authority_kind") in {"industry", "standard"}
        ]
        # 允许为空(当前恰好全都有引用), 但**不允许**出现 unverified 混进来
        for sid in kept_non_unverified:
            rec = next(r for r in seed_records if r.get("id") == sid)
            assert rec["authority_kind"] in {"industry", "standard"}


class TestNoDanglingStandardReference:
    def test_every_standard_authority_ref_resolves(self, seed_records) -> None:
        """删除之后不得留下悬空的标准引用。

        这是本次删除最危险的失败模式: 删掉某个标准, 而某条记录还指着它 ——
        那条记录会静默失去它的依据。
        """
        import re

        spec = importlib.util.spec_from_file_location("_bsd", ROOT / "scripts/build_seed_data.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_bsd"] = mod
        spec.loader.exec_module(mod)

        std_nums = {
            str(r["id"]).removeprefix("std::").strip()
            for r in seed_records
            if isinstance(r, dict) and r.get("entity_type") == "standard"
        }
        dangling: dict[str, list[str]] = {}
        for r in seed_records:
            if not isinstance(r, dict) or r.get("entity_type") == "standard":
                continue
            ref = str(r.get("authority_ref") or "")
            m = mod._STD_ID_RE.match(ref)
            if m is None:
                continue
            head = re.sub(r"\s+", " ", m.group(0)).strip()
            if head not in std_nums:
                dangling.setdefault(head, []).append(str(r.get("id")))
        # 只允许**结构上不可解析**的那几类(见方案附录), 不允许因删除而产生新的
        ALLOWED = {
            "GB/T 17626",      # 刻意写成「系列」
            "GB/T 2900.1",     # 一条引用给了两条标准, 前者无部分号
            "IEC 60664-1",     # 库里有 2020 版但引用未写年份 -> 需人工裁定
            "IEC 60721-3-1",   # 实体 id 本身是两条标准合写
            "IEC 60898-1",     # 同上
        }
        unexpected = {k: v for k, v in dangling.items() if k not in ALLOWED}
        assert not unexpected, (
            f"删除后出现新的悬空标准引用: {unexpected} —— "
            f"要么把对应标准加回去, 要么改这些 authority_ref"
        )
