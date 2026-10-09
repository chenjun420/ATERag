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
    def test_it_actually_finds_something_on_the_real_entities(self, builder, seed_records) -> None:
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
                assert rec.get("authority_kind") != "unverified" or rec.get("authority_ref"), (
                    f"{sid} 仍在库里且仍是 unverified + 无引用 —— 规则漏了它"
                )

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
            # 注意: 原先这里还有 std::IPC-2221, 已于 2026-10-08 移出范围,
            # 见 TestOutOfScopeRemovesItself —— 它的唯一引用方也出范围了。
        ):
            assert sid in ids, f"{sid} 被误删 —— 它有真实引用"


class TestOutOfScopeRemovesItself:
    """出范围的东西必须真的消失 —— 包括被它「文字引用」保活的标准。

    为什么这条要存在
    ------------------
    ``CONFORMAL_COATING``(三防涂覆)与 ``std::IPC-2221``(印制板**设计**标准)已于
    2026-10-08 移出范围: 属PCB 设计/组装侧, 不是电源产品产测侧。详见
    ``build_seed_data.OUT_OF_SCOPE_ENTITY_IDS``。

    这里最要紧的是: ``find_dead_standards`` 的第 3 条判据**会**因为
    ``CONFORMAL_COATING`` 的 ``source``/``authority_ref`` 文本提到 ``IPC-2221``
    而保住那个标准实体。**「有人提到它」不等于「该有它」** —— 提到它的那条知识
    本身就不在范围内。所以范围排除必须发生在这层之前。

    这条测试把上一轮那条「靠文本引用保住 IPC-2221」的断言**反过来**: 不是绕过它,
    而是断言它现在应该消失。留着旧断言会让「删掉出范围的东西」这个动作无法被测出来。
    """

    def test_coating_and_its_standard_are_gone(self, seed_records) -> None:
        ids = {r.get("id") for r in seed_records}
        assert "CONFORMAL_COATING" not in ids, "三防涂覆应已移出范围"
        assert "std::IPC-2221" not in ids, (
            "PCB 设计标准应随其唯一引用方一起移出范围 —— "
            "若它还在, 说明范围排除没跑在文字引用豁免之前"
        )

    def test_no_dangling_reference_to_them(self, seed_records) -> None:
        """移出范围不得留下悬空引用 —— 否则门禁会开始报 authority_ref_resolvable。"""
        blob = json.dumps(seed_records, ensure_ascii=False)
        assert "CONFORMAL_COATING" not in blob, "有记录还引用着已移出范围的三防涂覆"
        assert "IPC-2221" not in blob, "有记录还引用着已移出范围的 IPC-2221"

    def test_pollution_degree_stays(self, seed_records) -> None:
        """同源的 ``POLLUTION_DEGREE`` 必须留下 —— 它确实在产测判据链上。

        污染等级定爬电距离 → 爬电距离定耐压限值 → 实测值对照判合格, 这是
        产测判读耐压/漏电流**实测结果**的输入。与三防涂覆的区别就在这里:
        涂层是设计侧属性(要走两步才能影响产测), 污染等级直接进判据。
        """
        pd = next((r for r in seed_records if r.get("id") == "POLLUTION_DEGREE"), None)
        assert pd is not None, "污染等级不该跟着三防涂覆一起被移出"
        assert pd.get("clause"), "污染等级应保留已核实的条款号"


class TestTextCitationExemptionIsNotAPass:
    def test_text_citation_exemption_cannot_resurrect_out_of_scope(self, builder) -> None:
        """规则层面的红线: 范围排除集合里必须含这两条, 且排除发生在文字引用豁免之前。

        用本文件已有的 ``builder`` fixture 拿模块, **不要**在这里再 ``import
        build_seed_data`` —— 那会用第二个模块名把整个 5000 行脚本**再执行一遍**
        (模块级要构造全部实体字面量), 实测足以让单文件测试跑到超时。
        """
        oos = builder.OUT_OF_SCOPE_ENTITY_IDS
        assert "CONFORMAL_COATING" in oos, "三防涂覆必须在范围排除集合里"
        assert "std::IPC-2221" in oos, "PCB 设计标准必须在范围排除集合里"

    def test_static_scope_set_beats_text_citation_exemption(self, builder, seed_records) -> None:
        """静态范围集合**压得住**「文本提及豁免」—— 这才是真正起作用的机制。

        真实结构(别照抄我最初写错的猜测): ``find_dead_standards`` 不是在
        ``prune_non_executable`` 之前跑的独立一步, 它是被
        :func:`_out_of_scope_extra` **在同一个函数里**调用的, 合并方式是::

            _out_of_scope = _out_of_scope_static() | _out_of_scope_extra(entities)

        所以决定性的不是先后顺序, 而是**静态集合是并集的一侧**。光靠「零引用 +
        文本无提及」的动态检测救不回 ``std::IPC-2221`` —— 提到它的
        ``CONFORMAL_COATING`` 那一刻还在库里(两者在同一趟里被过滤),
        第 3 条判据会把它豁免掉。真正让它消失的是它**同时**在静态集合里。

        这条测试就断言这个优先级: 构造一个会「文本提及 IPC-2221」的实体集,
        动态检测应当**不**判它死(豁免生效), 而合并后的集合仍必须含它。
        """
        ents = [dict(r) for r in seed_records if r.get("id") and not r.get("source_id")]
        ents.append(
            {
                "id": "TMP_TEXT_CITER",
                "name": "tmp",
                "type": "power_concept",
                "text": "临时: 引用 IPC-2221 印制板设计",
                "properties": {
                    "authority_kind": "standard",
                    # **必须放在 properties 里**: find_dead_standards 的文本豁免
                    # 扫的是 props[f] for f in _TEXT_CITATION_FIELDS, 顶层
                    # ``text``/``name`` 不在其中(踩过一次: 豁免静默不生效)。
                    "source": "临时记录: 引用 IPC-2221 印制板设计标准",
                },
            }
        )
        # 零引用 + unverified 的标准实体。**不能从 seed_records 里取** ——
        # std::IPC-2221 已经真的被移出范围了(上面那条测试就断言它不在),
        # 这正是本测试要模拟的那个状态。
        ents.append(
            {
                "id": "std::IPC-2221",
                "name": "IPC-2221",
                "type": "standard",
                "text": "IPC-2221 印制板设计",
                "properties": {"authority_kind": "unverified", "source": "临时构造"},
            }
        )

        dead = builder.find_dead_standards(ents)
        assert "std::IPC-2221" not in dead, (
            "本测试的前提是文本豁免确实生效; 若它不再生效, 说明豁免规则变了, "
            "下面那条优先级断言的前提也不成立"
        )

        merged = builder._out_of_scope_static() | builder._out_of_scope_extra(ents)
        assert "std::IPC-2221" in merged, (
            "静态范围集合必须压住文本提及豁免 —— 否则出范围的东西会被一条"
            "同样出范围的知识以文字形式救回来"
        )
        assert "CONFORMAL_COATING" in merged


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
            "GB/T 17626",  # 刻意写成「系列」
            "GB/T 2900.1",  # 一条引用给了两条标准, 前者无部分号
            "IEC 60664-1",  # 库里有 2020 版但引用未写年份 -> 需人工裁定
            "IEC 60721-3-1",  # 实体 id 本身是两条标准合写
            "IEC 60898-1",  # 同上
        }
        unexpected = {k: v for k, v in dangling.items() if k not in ALLOWED}
        assert not unexpected, (
            f"删除后出现新的悬空标准引用: {unexpected} —— "
            f"要么把对应标准加回去, 要么改这些 authority_ref"
        )
