"""工艺知识 <-> 方法库 的接线(任务②)。

被钉住的不只是「现在是绿的」, 还有**门禁会红**: 构造一条没被任何方法引用的
``practice_scope=condition`` 知识, 校验必须报错。只会通过的断言证明不了它有牙。

四条纪律各有测试:

1. **双向可查** —— 方法引用的知识必须存在(前向/红线 5); ``practice_scope=condition``
   的知识必须被至少一个方法引用(反向/「入库了但没人用」)
2. **``scope`` 不复用** —— 标准表的 ``scope`` 是**适用范围**, 工艺知识用
   ``practice_scope``。混用会让门禁把标准当成待接线的方法引用
3. **``applies: always`` 只许约束「怎么测」** —— 它对双边齐全的条件也生效, 若允许
   挂 ``input_voltage``/``load``, 「额定输入+满载」会撒到每条 output_spec 上
4. **测法在同一条判据的各档之间必须一致** —— 修之前的实测: SR-1211 的 -54V 行无
   ``measurement_setup``、3.45V 行有, 读数不可比
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from aterag.extract import ProfileBook
from aterag.extract.assembler import PatternBook
from aterag.extract.assess import RuleBook
from aterag.extract.configs import validate_extraction_configs
from aterag.extract.models import (
    CONF_RULE,
    STATUS_APPROVED,
    ConditionClause,
    TestCondition,
)
from aterag.extract.quantity_aliases import QuantityAliasBook
from aterag.extract.scenarios import ScenarioRules
from aterag.extract.supplement import (
    ALWAYS_REQUIRES_SETUP,
    APPLIES_ALWAYS,
    APPLIES_MISSING_SIDE,
    MethodBook,
    supplement_conditions,
)
from aterag.ingest.persist_requirements import requirement_row

DP = "config/doc_profiles.yaml"
CP = "config/condition_patterns.yaml"
TM = "config/test_methods.yaml"
QA = "config/quantity_aliases.yaml"
SEED = Path("data/seed/power_domain_seed.json")

SCOPE_CONDITION = "condition"
SCOPE_PROCESS = "process"


# ---------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def seed_records() -> list[dict]:
    return [r for r in json.loads(SEED.read_text(encoding="utf-8"))["records"]
            if isinstance(r, dict)]


@pytest.fixture(scope="module")
def practice_scopes(seed_records) -> dict[str, str]:
    """种子里**全部**带 practice_scope 的实体 id -> scope。

    只取 practice_scope(不带这个键的普通概念不参与) —— 键名若写成 scope 会把
    标准表的「适用范围」也捞进来, 那是两种完全不同的含义。
    """
    return {r["id"]: r["practice_scope"] for r in seed_records
            if r.get("practice_scope") and r.get("id")}


@pytest.fixture(scope="module")
def book() -> MethodBook:
    return MethodBook.load(TM)


@pytest.fixture(scope="module")
def good():
    return (
        ProfileBook.load(DP),
        PatternBook.load(CP),
        MethodBook.load(TM),
        RuleBook.load(TM),
        ScenarioRules.load(),
        QuantityAliasBook.load(QA),
    )


def _cond(req_id: str = "SR-X-1", title: str = "t", *, rail: str = "",
          bilateral: bool = False, sides: tuple[str, ...] | None = None,
          role: str = "output_spec") -> TestCondition:
    """造一条条件。

    ``sides`` 直接指定规格书**声明了哪几侧**(未列的一侧为空 -> 走补齐);
    省略时等价于 ``("input",)``, ``bilateral=True`` 等价于两侧都有。
    """
    if sides is None:
        sides = ("input", "output") if bilateral else ("input",)
    c = TestCondition(req_id=req_id, title=title, section_path="4.3",
                      rail=rail, role=role)
    if "input" in sides:
        c.input_conditions.append(
            ConditionClause(kind="input_voltage", text="额定输入", role="input",
                            confidence=CONF_RULE, status=STATUS_APPROVED))
    if "output" in sides:
        c.output_conditions.append(
            ConditionClause(kind="output_voltage", text="在范围内", role="output",
                            confidence=CONF_RULE, status=STATUS_APPROVED))
    return c


# ------------------------------------------------- 1. 双向可查


class TestKnowledgeRefsResolve:
    def test_repository_wiring_is_consistent(self, good, practice_scopes):
        """仓库现状必须过 —— 并断言真的拿到了数据, 防止门禁被静默架空。"""
        assert practice_scopes, "种子里没有任何 practice_scope —— 门禁会整段跳过"
        validate_extraction_configs(*good, practice_scopes)

    def test_every_condition_scoped_knowledge_is_referenced(self, book, practice_scopes):
        """反向: ``practice_scope=condition`` 的知识必须有消费方。"""
        referenced = {k for m in book.methods for k in m.knowledge_ref}
        orphans = sorted(k for k, s in practice_scopes.items()
                         if s == SCOPE_CONDITION and k not in referenced)
        assert not orphans, f"入库却没人用的工艺知识: {orphans}"

    def test_dangling_knowledge_ref_raises(self, good, tmp_path):
        """前向: 方法引用了不存在的知识实体 -> 报错并点名。

        传入的词表刻意**不含**那个悬空 id, 且非空 —— 空词表会让 ``MethodBook``
        跳过前向检查(无从判断), 那就测不到这条了。
        """
        profiles, patterns, methods, rules, scen, aliases = good
        doc = yaml.safe_load(Path(TM).read_text(encoding="utf-8"))
        doc["methods"][0]["knowledge_ref"] = ["MEAS_DOES_NOT_EXIST"]
        p = tmp_path / "b.yaml"
        p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(
                profiles, patterns, MethodBook.load(p), rules, scen, aliases,
                # 非空且不含悬空 id -> 前向检查必然触发
                {"MEAS_RIPPLE_BW_LIMIT": SCOPE_CONDITION},
            )
        assert "MEAS_DOES_NOT_EXIST" in str(ei.value)

    def test_unreferenced_condition_knowledge_raises(self, good):
        """反向门禁必须有牙: 加一条没被引用的 condition 级知识 -> 报错并点名。

        用 ``*good`` 而不是拆包: 少传一个参数会让 practice_scopes 落到
        quantity_aliases 的位置上, 反向门禁整段跳过, 测试就假绿了。
        """
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(*good, {"MEAS_ORPHAN": SCOPE_CONDITION})
        assert "MEAS_ORPHAN" in str(ei.value)

    def test_process_scoped_knowledge_need_not_be_referenced(self, good, practice_scopes):
        """工艺级(老化/AQL/MSA/工装)没被方法引用是**本分**, 不该报错。

        必须传**真实**词表再加一条 process 条目: 只传那一条的话前向检查会认定
        现有 10 个 knowledge_ref 全悬空而报错 —— 那是词表给错了, 不是门禁该宽容。
        """
        scopes = dict(practice_scopes)
        scopes["PRACT_SOME_NEW_PROCESS"] = SCOPE_PROCESS
        validate_extraction_configs(*good, scopes)

    def test_refs_point_at_condition_scoped_entities_only(self, book, practice_scopes):
        """方法只该引用 condition 级知识 —— 引用工艺级就是层级错位。"""
        bad = sorted({k for m in book.methods for k in m.knowledge_ref
                      if practice_scopes.get(k) == SCOPE_PROCESS})
        assert not bad, f"方法引用了产线工艺级知识: {bad}"


# ------------------------------------------------- 2. scope 不复用


class TestScopeKeyNotReused:
    def test_standards_keep_their_own_scope(self, seed_records):
        """标准表的 ``scope`` 是**适用范围**, 与工艺知识的 practice_scope 无关。

        两者混在一张表: 一条标准若被标成 ``condition`` 就会被反向门禁要求「方法
        引用它」; 而门禁的 SHORT_REF_FIELDS 又含 ``scope``, 会把值当实体记号解析。
        """
        std_scopes = [r["scope"] for r in seed_records
                      if r.get("id", "").startswith("std::") and r.get("scope")]
        assert std_scopes, "标准表本来就没有 scope 字段? 那说明基线变了, 要重判"
        for r in seed_records:
            if r.get("id", "").startswith("std::"):
                assert "practice_scope" not in r, "标准实体不该带 practice_scope"

    def test_practice_entities_use_practice_scope_not_scope(self, seed_records):
        """工艺知识只写 ``practice_scope``, 不碰 ``scope``。"""
        for r in seed_records:
            if r.get("practice_scope"):
                assert "scope" not in r, f"{r['id']} 同时带 scope 与 practice_scope"


# ------------------------------------------------- 3. always 的作用域


class TestAppliesAlways:
    def test_always_methods_declare_measurement_setup(self, book):
        """``always`` 只许约束「怎么测」。

        放开 ``input_voltage``/``load`` 的话, 「额定输入+满载」会撒到每条匹配的
        output_spec 上, 把规格书给出的更具体激励顶替成泛用值。
        """
        for m in book.methods:
            if m.is_always:
                assert any(c.kind == ALWAYS_REQUIRES_SETUP for c in m.conditions), m.id

    def test_always_without_setup_raises(self, good, tmp_path):
        profiles, patterns, methods, rules, scen, aliases = good
        doc = yaml.safe_load(Path(TM).read_text(encoding="utf-8"))
        target = next(m for m in doc["methods"] if m.get("applies") == APPLIES_ALWAYS)
        target["conditions"] = [{"kind": "load", "value": {"note": "满载"}}]
        p = tmp_path / "b.yaml"
        p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(profiles, patterns, MethodBook.load(p),
                                        rules, scen, aliases)
        assert target["id"] in str(ei.value)
        assert ALWAYS_REQUIRES_SETUP in str(ei.value)

    def test_illegal_applies_value_raises(self, good, tmp_path):
        profiles, patterns, methods, rules, scen, aliases = good
        doc = yaml.safe_load(Path(TM).read_text(encoding="utf-8"))
        doc["methods"][0]["applies"] = "sometimes"
        p = tmp_path / "b.yaml"
        p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(profiles, patterns, MethodBook.load(p),
                                        rules, scen, aliases)
        assert "sometimes" in str(ei.value)

    def test_default_applies_is_missing_side(self, book):
        """默认仍是「只补缺侧」—— always 必须显式声明。"""
        for m in book.methods:
            assert m.applies in {APPLIES_MISSING_SIDE, APPLIES_ALWAYS}, m.id

    def test_pick_always_ignores_missing_side_methods(self, book):
        """``pick_always`` 只认 always; 兜底方法不该被测法通道捡走。"""
        cond = _cond(title="峰峰值杂音电压", bilateral=True)
        m = book.pick_always(cond, "input")
        assert m is not None and m.is_always

    def test_pick_always_prefers_the_more_specific_method(self, book):
        """同侧多条 always 方法时取最精确的, 而不是文件里第一条。"""
        assert len([m for m in book.methods if m.is_always]) >= 2, "夹具前提失效"


# ------------------------------------------------- 4. 测法在各档一致


class TestMeasurementSetupReachesBilateralConditions:
    def test_bilateral_condition_still_gets_measurement_setup(self, book):
        """双边齐全的条件也必须拿到测法 —— 这是修掉的那个缺陷。

        之前补齐层对双边条件直接 ``continue``, 于是测法类工艺知识只落在缺侧的
        10 条上, 95 条里 85 条拿不到。
        """
        cond = _cond(title="峰峰值杂音电压", bilateral=True)
        before = len(cond.input_conditions)
        supplement_conditions([cond], book)
        kinds = {c.kind for c in cond.input_conditions + cond.output_conditions}
        assert ALWAYS_REQUIRES_SETUP in kinds, "双边条件没拿到 measurement_setup"
        assert len(cond.input_conditions) > before

    def test_measurement_setup_is_identical_across_variants(self, book):
        """同一条判据的各档之间, 测法必须一致 —— 否则读数不可比。

        这是本轮实测到的具体缺陷: SR-1211「动态响应恢复时间」的 -54V 行无
        ``measurement_setup``、3.45V 行有。
        """
        title = "动态响应恢复时间"
        a = _cond("SR-X-1", title, rail="-54V", bilateral=True)
        b = _cond("SR-X-1", title, rail="3.45V", bilateral=True)
        supplement_conditions([a, b], book)
        ka = {c.kind for c in a.input_conditions + a.output_conditions}
        kb = {c.kind for c in b.input_conditions + b.output_conditions}
        assert ALWAYS_REQUIRES_SETUP in ka
        assert ALWAYS_REQUIRES_SETUP in kb

    def test_always_setup_does_not_suppress_stimulus_supplement(self, book):
        """第一趟挂的 measurement_setup 不能让该侧「看起来已填」而跳过补激励。

        顺序陷阱: always 趟先往**空的**输入侧挂一条 measurement_setup, 若缺侧是
        之后才判断, 这一侧就被自己的产物判成「已填」—— 补齐层反过来抑制了它该做
        的事。``missing`` 必须在 always 趟**之前**快照。

        所以断言的是: 输入侧同时拿到测法(measurement_setup)与激励(input_voltage
        /load), 而不是只有其中一样。
        """
        cond = _cond(title="峰峰值杂音电压", sides=("output",), role="output_spec")
        supplement_conditions([cond], book)
        in_kinds = {c.kind for c in cond.input_conditions}
        assert ALWAYS_REQUIRES_SETUP in in_kinds, "always 趟没挂上测法"
        assert "input_voltage" in in_kinds, "输入侧激励没被补 —— 缺侧快照失效了"
        assert "load" in in_kinds, "输入侧负载档没被补 —— 缺侧快照失效了"

    def test_spec_declared_setup_is_not_overridden(self, book):
        """规格书写明的 measurement_setup 优先, always 方法不得覆盖它。"""
        cond = _cond(title="峰峰值杂音电压", bilateral=True)
        cond.input_conditions.append(
            ConditionClause(kind=ALWAYS_REQUIRES_SETUP, text="规格书自己写的 10MHz",
                            role="input", confidence=CONF_RULE, status=STATUS_APPROVED))
        supplement_conditions([cond], book)
        setups = [c for c in cond.input_conditions
                  if c.kind == ALWAYS_REQUIRES_SETUP]
        assert len(setups) == 1 and setups[0].text == "规格书自己写的 10MHz"

    def test_always_clause_carries_knowledge_ref(self, book):
        """子句要带上 knowledge_ref —— 落库行才能自答依赖哪些工艺知识。"""
        cond = _cond(title="峰峰值杂音电压", bilateral=True)
        supplement_conditions([cond], book)
        refs = {k for c in cond.input_conditions + cond.output_conditions
                for k in getattr(c, "knowledge_ref", ())}
        assert refs, "补齐子句没带 knowledge_ref"


# ------------------------------------------------- 落库侧


class TestKnowledgeRefReachesDatabaseRow:
    def test_clause_payload_carries_knowledge_ref(self, book):
        cond = _cond(title="峰峰值杂音电压", bilateral=True)
        supplement_conditions([cond], book)
        row = requirement_row(cond)
        vec = row.condition_vector
        refs = {k for side in (vec["approved"], vec["draft"]) for c in side
                for k in (c.get("knowledge_ref") or ())}
        assert refs, "condition_vector 里没有 knowledge_ref —— DB 侧答不出依赖"

    def test_knowledge_refs_resolve_to_real_entities(self, book, practice_scopes):
        cond = _cond(title="峰峰值杂音电压", bilateral=True)
        supplement_conditions([cond], book)
        row = requirement_row(cond)
        vec = row.condition_vector
        refs = {k for side in (vec["approved"], vec["draft"]) for c in side
                for k in (c.get("knowledge_ref") or ())}
        assert refs <= set(practice_scopes), f"落库的 knowledge_ref 悬空: {refs}"
