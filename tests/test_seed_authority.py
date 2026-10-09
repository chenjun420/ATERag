"""种子数据的不变量测试。

这些断言对应的都是**已经真实发生过的缺陷**, 不是假想:
每条测试名里写清「原本坏在哪」, 免得将来有人把它当过度约束删掉。

范围: 只测 ``data/seed/power_domain_seed.json`` 的静态性质, 不碰 PG。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "seed" / "power_domain_seed.json"


@pytest.fixture(scope="module")
def data() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def records(data: dict) -> list[dict]:
    return data["records"]


@pytest.fixture(scope="module")
def by_id(records: list[dict]) -> dict[str, dict]:
    return {r["id"]: r for r in records if r.get("id")}


# ---------------------------------------------------------------------------
# 缺陷 #12: 别名列落到无人读的标量字段
# ---------------------------------------------------------------------------


def test_no_record_uses_the_dead_aliases_field(records: list[dict]) -> None:
    """概念字典第 4 列曾写成标量 ``aliases``, 而**没有任何消费方读它**。

    schema 声明的检索字段是 ``synonyms``(数组), 遥测族处理器也写 ``synonyms``。
    于是 118 条概念的另一种叫法对检索完全不可见, 而检索照样返回结果 ——
    少召回是隐形的, 看不出来。
    """
    offenders = [r["id"] for r in records if r.get("aliases")]
    assert not offenders, f"aliases 字段复活了(无人读取): {offenders[:5]}"


def test_concept_synonyms_are_lists_not_joined_strings(records: list[dict]) -> None:
    """别名列一格里可能有多个别名(实测 22 条), 必须拆成数组。"""
    bad = [
        r["id"]
        for r in records
        if isinstance(r.get("synonyms"), str)
        or (
            isinstance(r.get("synonyms"), list)
            and any(isinstance(x, str) and ("," in x or "、" in x) for x in r["synonyms"])
        )
    ]
    assert not bad, f"synonyms 里残留未拆分的逗号串: {bad[:5]}"


def test_alias_split_does_not_break_slash_containing_aliases(by_id: dict) -> None:
    """分隔符**不含** ``/`` —— 实测存在别名本身带斜杠(``EFT/B``)。

    切斜杠会把一个别名劈成两个不存在的别名, 而检索时两个都命中不了真词。
    样本: ``EFT_IMMUNITY`` 的别名列原文是 ``电快速瞬变, EFT/B``, 应拆成两项
    且 ``EFT/B`` 原样保留。
    """
    syn = by_id["EFT_IMMUNITY"]["synonyms"]
    assert "EFT/B" in syn, f"带斜杠的别名被切开了: {syn}"
    assert "电快速瞬变" in syn, f"同格的另一个别名丢了: {syn}"


def test_synonyms_have_no_duplicates_and_no_blanks(records: list[dict]) -> None:
    """同义词表自身要干净: 空串会让检索匹配到一切, 重复项只是噪声。"""
    bad = []
    for r in records:
        syn = r.get("synonyms")
        if not isinstance(syn, list):
            continue
        if any(not isinstance(x, str) or not x.strip() for x in syn):
            bad.append((r["id"], "空串/非字符串"))
        elif len(set(syn)) != len(syn):
            bad.append((r["id"], "重复"))
    assert not bad, bad[:5]


# ---------------------------------------------------------------------------
# 回差 与 返回系数 不可混为一谈
# ---------------------------------------------------------------------------


def test_hysteresis_exists_with_standard_authority(by_id: dict) -> None:
    """用户点名要查的词。确认它在库里, 且权威不是 unverified。"""
    r = by_id.get("HYSTERESIS")
    assert r is not None, "回差概念缺失"
    assert r["zh"] == "回差"
    assert r["authority_kind"] == "standard", f"回差权威={r['authority_kind']}, 应为 standard"
    assert r.get("authority_ref"), "回差没有 authority_ref"
    assert r.get("confidence") is not None, "confidence 为 None 等于宣称「已验证」"


def test_return_ratio_concepts_carry_no_hysteresis_synonym(by_id: dict) -> None:
    """返回系数是**比值**, 回差是**差值** —— 互换会让保护整定算错。

    方案 md 原本给 ``SET_*_RETURN`` 加了别名「OCP回差/OVP回差/OTP回差」,
    已按 GB/T 14598.127-2013 3.10 删除。
    """
    for cid in ("SET_OCP_RETURN", "SET_OVP_RETURN", "SET_OTP_RETURN"):
        r = by_id[cid]
        syn = r.get("synonyms") or []
        assert not any("回差" in s for s in syn), f"{cid} 别名里仍混着回差: {syn}"
        assert r.get("standard_term") == "复归系数", f"{cid} 缺标准正名"
        assert r.get("clause") == "3.10", f"{cid} 缺条号"
        assert r.get("authority_kind") == "standard", f"{cid} 权威={r.get('authority_kind')}"


def test_return_ratio_synonyms_cover_both_name_pairs_of_its_clause(by_id: dict) -> None:
    """GB/T 14598.127-2013 3.10 同一词条给了两套名字:

    ``3.10 复归系数 reset ratio  返回系数 disengaging ratio``

    两套都要能检索到 —— 按「跨标准同义名并入 synonyms」处理。
    """
    syn = set(by_id["SET_OCP_RETURN"]["synonyms"])
    for want in ("复归系数", "reset ratio", "返回系数", "disengaging ratio"):
        assert any(want in s for s in syn), f"同义词缺 {want}: {syn}"


# ---------------------------------------------------------------------------
# 新增实体(concepts_add / standards_add)的出处纪律
# ---------------------------------------------------------------------------


def test_added_records_all_carry_authority_and_provenance(data: dict, records: list[dict]) -> None:
    """新增段的存在意义就是「方案 md 没有的、按标准补进来的」知识。

    这类知识最容易变成无出处数据 —— 而无出处的修正比不修正更危险:
    它看起来可信, 却无法复核。所以逐条查 authority_ref / confidence / provenance。
    """
    applied = data["provenance"]["corrections_applied"]
    created = [a for a in applied if a.get("action") == "created"]
    assert created, "corrections_applied 里没有 created 记录 —— 新增段没生效?"
    by = {r["id"]: r for r in records if r.get("id")}
    problems = []
    for a in created:
        r = by.get(a["id"])
        if r is None:
            problems.append((a["id"], "记录不存在"))
            continue
        if not r.get("authority_ref"):
            problems.append((a["id"], "authority_ref 为空"))
        if r.get("authority_kind") not in ("standard", "book", "industry", "project_defined"):
            problems.append((a["id"], f"authority_kind={r.get('authority_kind')}"))
        if r.get("confidence") in (None, 0):
            problems.append((a["id"], "confidence 为空"))
        if not (r.get("provenance") or {}):
            problems.append((a["id"], "provenance 为空"))
    assert not problems, problems


def test_added_concepts_declare_the_standard_they_came_from(
    data: dict, records: list[dict]
) -> None:
    """声称来自标准的知识, authority_ref 必须是**标准号**, 不能是那段 URL。

    这条对应一次真实回归: 新增段只写了 ``source``(URL) 没写 ``standard_ref``,
    ``_authority_of`` 便落到兜底分支, 于是 36 条来自 YD/T 的术语被标成
    ``authority_kind=industry``、``authority_ref`` 变成 URL —— 声称来自标准的
    知识实际标成了业界说法, 还断开了到标准实体的 defined_by 链接。
    """
    by = {r["id"]: r for r in records if r.get("id")}
    for a in data["provenance"]["corrections_applied"]:
        if a.get("action") != "created" or a.get("kind") != "power_concept":
            continue
        ref = by[a["id"]].get("authority_ref")
        assert ref and not ref.startswith("http"), f"{a['id']} 的 authority_ref 是 URL: {ref}"
        assert by[a["id"]]["authority_kind"] == "standard", f"{a['id']} 未标为 standard"


def test_superseded_standards_are_marked_not_left_current(data: dict, records: list[dict]) -> None:
    """「以现行版为准」要真的落到 status 上, 不能只是加一条现行版就完事 ——
    否则同一标准会有两个 CURRENT, 消费方无从判断该引哪个。"""
    stds = {r["id"]: r for r in records if r.get("entity_type") == "standard"}
    assert stds["std::YD/T 1817-2026"]["status"] == "CURRENT"
    assert stds["std::YD/T 1817-2017"]["status"] == "SUPERSEDED"
    assert stds["std::YD/T 1817-2017"].get("replaced_by") == "YD/T 1817-2026"
    # 被替代关系必须双向可追, 否则换版后 2017 版就成了孤儿
    assert stds["std::YD/T 1817-2026"].get("replaces") == "YD/T 1817-2017"


def test_clause_numbers_only_where_a_source_was_read(records: list[dict]) -> None:
    """给了条号的必须同时有**可核对的来源** —— 否则条号就是编的。

    核对点是 ``source.metadata.correction_source``(真实 URL/文本出处)。
    ``source.document`` 只是标准号, 拿它当「有出处」等于自己判自己及格。

    反向不成立: 没给条号的条目合法 —— 按「未查到条号时只给标准号」的规矩,
    查不到就不写, 不硬凑。
    """
    bad = []
    for r in records:
        if not r.get("clause") or not r.get("id"):
            continue  # 关系记录也带 clause, 它们没有 id
        prov = r.get("provenance") or {}
        srcs = [s for p in prov.values() for s in (p.get("sources") or [])]
        if not any((s.get("metadata") or {}).get("correction_source") for s in srcs):
            bad.append(r["id"])
    assert not bad, f"有条号但查不到核对点(correction_source): {bad[:5]}"


def test_records_without_clause_still_declare_the_standard(records: list[dict]) -> None:
    """「未查到条号时只给标准号」—— 给了标准号就必须真的给了。

    这类条目(实测 9 条)若连标准号都没有, 就退回成了无出处数据。
    """
    bad = [
        r["id"]
        for r in records
        if r.get("id") and r.get("authority_kind") == "standard" and not r.get("authority_ref")
    ]
    assert not bad, f"标为 standard 却没有 authority_ref(标准号): {bad[:5]}"
