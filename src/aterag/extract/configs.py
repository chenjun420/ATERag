"""抽取侧配置的**单一加载 + 校验入口**。

为什么要有这个模块 (方案 §11.6 A20)
------------------------------------
``doc_profiles.yaml`` 与 ``test_methods.yaml`` 是两份互相引用的配置: 方法库用
``applies_to.role`` 指认「这条方法适用于哪类章节角色」, 而角色名是**档案的
知识**(写在 ``section_priors.role`` 里), 代码里没有硬编码的角色表。

自洽性校验过去只发生在**抽取调用内部**, 于是这类断裂的表现是:

```
ValueError: 业界方法库与条件词表不自洽:
  methods[psu_output_default_stimulus].applies_to.role 非法: output_spec
```

人只在**跑抽取**时才看到 —— 而他刚做的事是改配置。配置能加载成功却不能用于
抽取, 这本身就是缺陷: 「加载成功」被当成了「配置可用」(红线 14 同类误读)。

于是把校验挪到**加载期**, 并收敛到一个函数:

- :func:`load_extraction_configs` —— 加载五份配置并校验, 调用方拿到的
  一定是可用的一套;
- :func:`validate_extraction_configs` —— 校验本体, 纯函数、零 IO, 供
  ``scripts/validate_configs.py`` 与单测复用。**只有这一份实现**: 校验规则
  曾经有两份(脚本一份、抽取一份), 两份必然会漂。

依赖纪律: 本模块只 import 同包内的配置类与标准库, 不碰 DB / 网络, 所以
``health``、离线脚本、单测都能跑它。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aterag.extract.assembler import PatternBook
from aterag.extract.assess import RuleBook
from aterag.extract.quantity_aliases import QuantityAliasBook
from aterag.extract.scenarios import ScenarioRules
from aterag.extract.supplement import MethodBook

if TYPE_CHECKING:  # 运行时不需要 Settings, 只有类型标注要用
    from aterag.config import Settings

#: ``section_priors.limits_to`` 的合法值 (与 ``ScenarioRules`` 的用法一致)。
#: 放在这里是因为「限值归哪一侧」也是档案的知识, 但**取值空间是封闭的** ——
#: 写错不会报错, 只会让条件永远归错侧, 所以要在加载期挡住。
LIMITS_TO_VALUES = frozenset({"input", "output", "both"})


@dataclass(frozen=True, slots=True)
class ExtractionConfigs:
    """抽取侧六份配置, 已通过 :func:`validate_extraction_configs`。"""

    profiles: object  # ProfileBook (延迟标注: 它在本模块下游, 避免循环 import)
    patterns: PatternBook
    methods: MethodBook
    assess_rules: RuleBook
    scenario_rules: ScenarioRules
    quantity_aliases: "QuantityAliasBook | None" = None


def role_vocabulary(profiles: object) -> frozenset[str]:
    """档案里出现过的全部章节角色名 (方法库 ``applies_to.role`` 的合法集合)。

    **从档案取而不是硬编码**: 角色是文档认知, 换产品线换档案而非换代码。
    """
    return frozenset(
        pr.role  # type: ignore[attr-defined]
        for p in getattr(profiles, "profiles").values()  # type: ignore[attr-defined]
        for pr in p.section_priors.values()
    )


def validate_extraction_configs(
    profiles: object,
    patterns: PatternBook,
    methods: MethodBook,
    assess_rules: RuleBook,
    scenario_rules: ScenarioRules | None = None,
    quantity_aliases: QuantityAliasBook | None = None,
) -> None:
    """校验六份配置互相自洽, 不自洽就抛 (fail-closed)。

    校验项按「错了会怎样」排:
    1. 档案自身: 没有 ``section_keywords`` 的档案选中不了章节; ``limits_to``
       写错会让限值永远归错侧;
    2. 方法库 <-> 词表/角色: 引用词表外的 ``kind`` 下游无法翻译执行动作,
       引用档案没有的 ``role`` 则永远匹配不上;
    3. 评估规则与场景规则: 引用不存在的方法 id / 维度名;
    4. 标题别名表: 别名跨事实互斥 —— 同一条标题认两个事实时结果只取决于遍历
       顺序, 那是巧合不是判断 (§11.7)。
    """
    problems: list[str] = []

    book = getattr(profiles, "profiles", {})
    default = getattr(profiles, "default_profile", "")
    if not book:
        problems.append("doc_profiles.yaml 未定义任何 profile")
    elif default not in book:
        problems.append(f"default_profile 指向未定义档案: {default}")
    for name, prof in book.items():
        if not prof.section_keywords:
            problems.append(f"profile {name} 未定义 section_keywords (选中不了任何章节)")
        for sec, pr in prof.section_priors.items():
            if pr.limits_to not in LIMITS_TO_VALUES:
                problems.append(f"profile {name} 的 {sec}.limits_to 非法: {pr.limits_to}")

    roles = role_vocabulary(profiles)
    try:
        methods.validate(patterns.kinds, roles)
        methods.validate_templates()
    except ValueError as e:
        problems.append(f"test_methods.yaml: {e}")
    try:
        assess_rules.validate()
    except ValueError as e:
        problems.append(f"评估规则: {e}")
    if scenario_rules is not None and not scenario_rules.dimensions:
        problems.append("scenario_rules.yaml 未定义任何场景维度 (不拆场景, 判据含混)")
    if quantity_aliases is not None:
        try:
            quantity_aliases.validate()
        except ValueError as e:
            problems.append(f"quantity_aliases.yaml: {e}")

    if problems:
        raise ValueError("抽取侧配置不自洽: " + "; ".join(problems))


def load_extraction_configs(settings: "Settings | None" = None) -> ExtractionConfigs:
    """按 ``settings`` 的路径加载六份抽取配置并校验。

    路径全部来自配置(不写死相对路径), 因为板卡上部署目录与开发机不同 ——
    这正是历史上 ``validate_configs.py`` 找不到 ``registry.yaml`` 的那类坑。
    """
    from aterag.config import get_settings
    from aterag.extract.api import ProfileBook

    s = settings or get_settings()
    profiles = ProfileBook.load(s.doc_profiles_path)
    patterns = PatternBook.load(s.condition_patterns_path)
    methods = MethodBook.load()
    assess_rules = RuleBook.load(methods.path)
    scenario_rules = ScenarioRules.load()
    aliases = QuantityAliasBook.load(s.quantity_aliases_path)
    validate_extraction_configs(profiles, patterns, methods, assess_rules, scenario_rules, aliases)
    return ExtractionConfigs(
        profiles=profiles,
        patterns=patterns,
        methods=methods,
        assess_rules=assess_rules,
        scenario_rules=scenario_rules,
        quantity_aliases=aliases,
    )
