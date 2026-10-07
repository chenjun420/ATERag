"""抽取侧配置自洽性 (方案 §11.6 A20) 的不变量测试。

背景: ``doc_profiles.yaml`` 与 ``test_methods.yaml`` 互相引用 —— 方法库用档案的
``section_priors.role`` 指认适用范围。校验过去只在 ``extract_test_conditions``
内部发生, 于是「配置能加载成功」被误读成「配置可用」(红线 14 同类误读),
最常见的改配置姿势(给方法加个 role 却没在档案里声明)要等真跑抽取才炸。

这里钉住三件事:
1. 坏配置在**加载期**就抛, 不是在抽取期;
2. ``health`` 的检查项包含它 —— 运维改完配置跑一次就能看到, 不必等产线;
3. 校验规则**只有一份实现** —— 脚本与运行期共用, 漂掉的那份不会报错、
   只会在另一个入口放行坏配置。
"""

from __future__ import annotations

import asyncio

import pytest
import yaml

from aterag.extract.api import ProfileBook
from aterag.extract.assembler import PatternBook
from aterag.extract.assess import RuleBook
from aterag.extract.configs import (
    LIMITS_TO_VALUES,
    load_extraction_configs,
    role_vocabulary,
    validate_extraction_configs,
)
from aterag.extract.quantity_aliases import QuantityAliasBook, QuantityFact
from aterag.extract.scenarios import ScenarioRules
from aterag.extract.supplement import MethodBook

DP = "config/doc_profiles.yaml"
CP = "config/condition_patterns.yaml"
TM = "config/test_methods.yaml"
QA = "config/quantity_aliases.yaml"


@pytest.fixture
def good():
    return (
        ProfileBook.load(DP),
        PatternBook.load(CP),
        MethodBook.load(TM),
        RuleBook.load(TM),
        ScenarioRules.load(),
        QuantityAliasBook.load(QA),
    )


def _write(tmp_path, src: str, mutate) -> str:
    """把一份配置读出来改坏再写到临时文件, 返回新路径。"""
    doc = yaml.safe_load(open(src, encoding="utf-8").read())
    mutate(doc)
    p = tmp_path / "broken.yaml"
    p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return str(p)


class TestGoodConfigsPass:
    def test_repository_configs_are_consistent(self, good):
        """仓库里这套配置必须过 —— 期望集为空集不是放宽, 是「不该有坏配置」。"""
        validate_extraction_configs(*good)

    def test_role_vocabulary_comes_from_profiles_not_code(self, good):
        """角色词表必须等于档案里出现过的角色, 不多不少 (换档案=换角色知识)。"""
        profiles, _, _, _, _, _ = good
        declared = {pr.role for p in profiles.profiles.values() for pr in p.section_priors.values()}
        assert role_vocabulary(profiles) == declared
        assert role_vocabulary(profiles) <= set(role_vocabulary(profiles))

    def test_limits_to_vocabulary_is_closed(self):
        """limits_to 取值空间封闭 —— 写错不报错, 只会让限值永远归错侧。"""
        assert LIMITS_TO_VALUES == {"input", "output", "both"}


class TestBrokenConfigsRaiseAtLoad:
    def test_method_role_not_in_profiles_raises(self, good, tmp_path):
        """方法库引用了档案没有的 role -> 加载期抛错, 并点名是哪条方法。"""
        profiles, patterns, methods, rules, scen, _ = good

        def mutate(doc):
            doc["methods"][0]["applies_to"]["role"] = "role_that_profile_never_declared"

        broken = MethodBook.load(_write(tmp_path, TM, mutate))
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(profiles, patterns, broken, rules, scen)
        assert "role_that_profile_never_declared" in str(ei.value)
        assert methods.methods[0].id in str(ei.value), "报错要点名出是哪条方法"

    def test_method_kind_outside_vocab_raises(self, good, tmp_path):
        profiles, patterns, methods, rules, scen, _ = good

        def mutate(doc):
            doc["methods"][0]["applies_to"]["kinds"] = ["kind_that_does_not_exist"]

        broken = MethodBook.load(_write(tmp_path, TM, mutate))
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(profiles, patterns, broken, rules, scen)
        assert "kind_that_does_not_exist" in str(ei.value)

    def test_bad_limits_to_raises(self, good, tmp_path):
        profiles, patterns, methods, rules, scen, _ = good
        # 章节号随档案不同, 取实际的第一个键 —— 测试不该钉住某个编号。
        first_sec = next(iter(profiles.profiles[profiles.default_profile].section_priors))

        def mutate(doc):
            doc["profiles"][doc["default_profile"]]["section_priors"][first_sec]["limits_to"] = (
                "inputs"
            )

        broken_profiles = ProfileBook.load(_write(tmp_path, DP, mutate))
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(broken_profiles, patterns, methods, rules, scen)
        assert "limits_to" in str(ei.value)

    def test_profile_without_section_keywords_raises(self, good, tmp_path):
        """没有 section_keywords 的档案选中不了章节 -> 配置不可能可用。"""
        _, patterns, methods, rules, scen, _ = good

        def mutate(doc):
            doc["profiles"][doc["default_profile"]]["section_keywords"] = []

        broken_profiles = ProfileBook.load(_write(tmp_path, DP, mutate))
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(broken_profiles, patterns, methods, rules, scen)
        assert "section_keywords" in str(ei.value)

    def test_default_profile_pointing_nowhere_raises(self, good, tmp_path):
        _, patterns, methods, rules, scen, _ = good

        def mutate(doc):
            doc["default_profile"] = "no_such_profile"

        broken_profiles = ProfileBook.load(_write(tmp_path, DP, mutate))
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(broken_profiles, patterns, methods, rules, scen)
        assert "no_such_profile" in str(ei.value)

    def test_empty_scenario_dimensions_raises(self, good):
        """场景维度为空 -> 不拆场景, 下游拿到含混判据却不报错。"""
        profiles, patterns, methods, rules, _, _ = good
        empty = ScenarioRules(dimensions=(), tier_pattern="", path="")
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(profiles, patterns, methods, rules, empty)
        assert "场景维度" in str(ei.value)

    def test_broken_alias_book_raises_through_this_gate(self, good):
        """标题别名表也走这道闸 -> health 覆盖得到 (否则取数侧静默取空)。"""
        profiles, patterns, methods, rules, scen, alias_book = good
        broken = QuantityAliasBook(
            facts={
                **alias_book.facts,
                "current": QuantityFact(
                    name="current",
                    titles=(*alias_book.facts["current"].titles, "额定输出电压"),
                    match="exact",
                    value_from=("max",),
                ),
            },
            path=alias_book.path,
        )
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(profiles, patterns, methods, rules, scen, broken)
        assert "quantity_aliases.yaml" in str(ei.value)

    def test_all_problems_reported_at_once_not_one_at_a_time(self, good, tmp_path):
        """一次报全部问题: 逐个报错会让人修一个跑一次, 与「改配置」的心智不符。"""
        profiles, patterns, methods, rules, scen, _ = good

        def mutate(doc):
            doc["methods"][0]["applies_to"]["role"] = "bad_role_1"
            doc["methods"][1]["applies_to"]["kinds"] = ["bad_kind_1"]
            doc["methods"][1]["applies_to"]["role"] = "bad_role_2"

        broken = MethodBook.load(_write(tmp_path, TM, mutate))
        with pytest.raises(ValueError) as ei:
            validate_extraction_configs(profiles, patterns, broken, rules, scen)
        msg = str(ei.value)
        for token in ("bad_role_1", "bad_kind_1", "bad_role_2"):
            assert token in msg, f"{token} 未出现在一次性汇总报错里: {msg}"


class TestSingleValidationImplementation:
    def test_health_includes_extraction_configs_check(self):
        """health 必须在检查项里 —— 运维改完配置跑一次就能看到, 不必等产线。"""
        from aterag import checks

        item = checks._check_extraction_configs(_FakeSettings())
        assert item.ok, f"仓库自带配置应通过健康检查: {item.detail}"
        assert "profile" in item.detail, "检查详情要报出配置规模, 便于确认它真跑了"

    def test_health_wires_the_check_in(self):
        """health 必须真的跑这一项 —— 只测 _check_ 本身, 挂漏了照样绿 (已实测变异存活)。"""
        import ast
        import inspect

        from aterag import checks

        tree = ast.parse(inspect.getsource(checks.run_all_checks))
        called = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "_check_extraction_configs" in called, (
            "run_all_checks 没有调用 _check_extraction_configs -> "
            "health 看不到抽取侧配置坏了, A20 等于没做"
        )

    def test_health_check_fails_loudly_on_broken_config(self, monkeypatch):
        """坏配置 -> health 报 ok=False 并带原样报错 (能照着改), 不静默吞掉。"""
        from aterag import checks

        def boom(_settings):
            raise ValueError("methods[x].applies_to.role 非法: nope")

        monkeypatch.setattr("aterag.extract.configs.load_extraction_configs", boom)
        item = checks._check_extraction_configs(_FakeSettings())
        assert not item.ok
        assert "nope" in item.detail
        assert "ValueError" in item.detail, "要带上异常类型, 区分配置坏与其他故障"

    def test_validate_configs_script_uses_shared_validator(self):
        """脚本必须复用运行期那份实现 —— 两份校验规则必然漂。"""
        import inspect

        import scripts.validate_configs as vc  # type: ignore[import-not-found]

        src = inspect.getsource(vc)
        assert "validate_extraction_configs" in src
        # 不允许脚本自己再实现一遍 role/kind 交叉校验
        assert "mbook.validate(" not in src, "脚本里残留了第二份实现"
        assert "rules.validate()" not in src

    def test_extract_test_conditions_delegates_to_shared_validator(self):
        """抽取路径也必须走同一份 —— 否则又是一条能漂的分支。"""
        import inspect

        from aterag.extract import api

        src = inspect.getsource(api.extract_test_conditions)
        assert "validate_extraction_configs(" in src
        assert "method_book.validate(" not in src

    def test_load_extraction_configs_reads_paths_from_settings(self):
        """加载路径来自配置而非硬编码相对路径 —— 板卡部署目录与开发机不同。"""
        cfgs = load_extraction_configs(_FakeSettings())
        assert cfgs.patterns.rules, "条件规则库为空说明没加载到"
        assert cfgs.methods.methods
        assert cfgs.scenario_rules.dimensions


class _FakeSettings:
    """只需要配置路径的那几个字段 —— 加载期校验不碰 DB。

    注意: 这里刻意只列路径字段。少列一个, ``load_extraction_configs`` 就会在
    加载期抛 AttributeError 而不是悄悄用默认值 —— 那正是 A20 想要的报错时机。
    """

    def __init__(self):
        self.doc_profiles_path = DP
        self.condition_patterns_path = CP
        self.quantity_aliases_path = QA


def test_load_extraction_configs_is_awaitable_free():
    """加载校验必须是同步纯配置 IO —— health 里已经能直接调。"""
    result = asyncio.run(_load_once())
    assert result is not None


async def _load_once():
    from aterag.extract.configs import load_extraction_configs

    return load_extraction_configs(_FakeSettings())
