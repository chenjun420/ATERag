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

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import TYPE_CHECKING, Any

from aterag.extract.assembler import PatternBook
from aterag.extract.assess import RuleBook
from aterag.extract.quantity_aliases import QuantityAliasBook
from aterag.extract.scenarios import ScenarioRules
from aterag.extract.supplement import MethodBook

if TYPE_CHECKING:  # 运行时不需要 Settings, 只有类型标注要用
    from aterag.config import Settings
    from aterag.extract.api import DocProfile

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
    practice_scopes: Mapping[str, str] | None = None,
) -> None:
    """校验六份配置互相自洽, 不自洽就抛 (fail-closed)。

    校验项按「错了会怎样」排:
    1. 档案自身: 没有 ``section_keywords`` 的档案选中不了章节; ``limits_to``
       写错会让限值永远归错侧;
    2. 方法库 <-> 词表/角色: 引用词表外的 ``kind`` 下游无法翻译执行动作,
       引用档案没有的 ``role`` 则永远匹配不上;
    3. 评估规则与场景规则: 引用不存在的方法 id / 维度名;
    4. 标题别名表: 别名跨事实互斥 —— 同一条标题认两个事实时结果只取决于遍历
       顺序, 那是巧合不是判断 (§11.7);
    5. 方法库 <-> 域知识: 双向, 这是「判据引用了工艺要求」可查的落点。
       前向 —— 方法引用的知识实体不存在, 则依据悬空(红线 5);
       **反向 —— ``scope=condition`` 的知识没被任何方法引用, 即「入库了但没人用」**。
       反向才是关键: 前向只防拼错, 反向才驱动补齐工作。
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
        # 模板身份缺失 -> template_identity 在抽取时抛, 那是**运行期**才发现;
        # 加载期就该挡住, 否则 health 看得到配置坏了却报"通过"。
        if not getattr(prof, "template_id", ""):
            problems.append(
                f"profile {name} 未定义 template_id (方案 §4.0①) —— 抽取时会抛"
                "「档案未声明 template_id」, 而模板身份是事后追责的前提"
            )
        if not getattr(prof, "template_version", ""):
            problems.append(f"profile {name} 未定义 template_version (方案 §4.0①)")
        for sec, pr in prof.section_priors.items():
            if pr.limits_to not in LIMITS_TO_VALUES:
                problems.append(f"profile {name} 的 {sec}.limits_to 非法: {pr.limits_to}")

    roles = role_vocabulary(profiles)
    # 域知识 <-> 方法库 的双向校验。
    #
    # ``practice_scopes`` 是「知识实体 id -> practice_scope」, 由调用方从种子 JSON
    # 读出。传 None 则**整段跳过**而不是判定通过: 离线环境可能没有种子, 那时报
    # 「知识悬空」是它无法判断的错。但反过来说, 在拿得到种子的环境里传 None 就是
    # 静默放弃这道门禁 —— 所以 ``scripts/validate_configs.py`` 必须真的传进来。
    known_knowledge = frozenset(practice_scopes or ())
    try:
        methods.validate(patterns.kinds, roles, known_knowledge)
        methods.validate_templates()
    except ValueError as e:
        problems.append(f"test_methods.yaml: {e}")
    if practice_scopes:
        # 反向: practice_scope=condition 的知识必须被至少一个方法引用。
        #
        # 这条是任务②的核心断言 —— 它把「工艺知识入库但没人用」从一句口头判断变成
        # 一条会失败的校验。practice_scope 正是为此存在: 产线工艺级(老化/AQL/MSA/
        # 工装)没被引用是本分, 混进这个集合报出来就全是噪声, 于是门禁被架空。
        referenced = {kr for m in methods.methods for kr in m.knowledge_ref}
        orphans = sorted(
            kid
            for kid, sc in practice_scopes.items()
            if sc == "condition" and kid not in referenced
        )
        if orphans:
            problems.append(
                f"域知识有 {len(orphans)} 条 practice_scope=condition 的工艺知识未被任何"
                f"方法引用: {orphans} —— 它们决定「判据怎么测」, 不接线就等于入库了但不起"
                "作用; 要么在 test_methods.yaml 里加方法并写 knowledge_ref, "
                "要么把 practice_scope 改成 process (确属产线工艺级)"
            )
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


# ---------------- 模板指纹 (方案 §4.0①) ----------------
#
# 为什么需要它
# ------------
# 「模板」这个概念此前**根本没被建模** —— 全库零命中。抽取实际由 profile +
# condition_patterns + table_schemas 共同决定, 但没有「模板身份」这个字段,
# 于是模板变更的后果全是静默失效: 章节号改了 → role 判错; 标题改了 → 条件数
# 变 0; 表头改了 → 实体抽不出。抽取照样跑完, 只是结果不对 (§4.0 表格)。
#
# 指纹解决的是「事后追责」与「相对基线漂移」两件事, 它**不能**替代抽取过程的
# fail-closed: 指纹变了只说明参数变了, 不说明结果错了 —— 而 §4.0 的核心结论是
# 检测归抽取过程。所以这里的定位是**记录 + 回归对账**, 不是检测主力。
#
# 为什么只取结构
# --------------
# 剔除 description / note 这类易变文案 (照 ``bundle.contract_hash`` 的
# ``_VOLATILE_SCHEMA_KEYS`` 做法)。否则改一句说明就要同步所有基线, 久了没人同步,
# 基线就烂了 —— 而烂掉的基线等于没有基线。

#: 参与指纹的档案字段 —— 结构性的, 改了会改变抽取行为。
FINGERPRINT_PROFILE_KEYS = (
    "template_id",
    "template_version",
    "section_keywords",
    "exclude_words",
    "include_prose",
    "default_role",
    "default_limits_to",
    "reference_markers",
    "req_id_pattern",
    "section_priors",
)

#: 明确排除的字段: 说明性文案, 改它不该让指纹变。
#: ``review_dispositions`` 也排除 —— 那是**人签字的处置记录**, 随评审推进而增,
#: 若进指纹则每签一条判据就等于换了一版模板, 基线会天天红。
FINGERPRINT_EXCLUDED_KEYS = frozenset({"description", "note", "review_dispositions", "name"})


def _stable(node: Any) -> str:
    """稳定序列化: dict 按键排序, 不含时间戳与易变文案。

    dataclass **必须先摊成 dict** 再排除键。直接 ``json.dumps(default=str)`` 会
    走 dataclass 的 repr, 于是它的每个字段都进哈希 —— 包括 ``SectionPrior.note``
    这种纯说明文字, 改一句注释就让模板指纹变。
    """
    if is_dataclass(node) and not isinstance(node, type):
        node = asdict(node)
    if isinstance(node, Mapping):
        return (
            "{"
            + ",".join(
                f"{k}:{_stable(node[k])}"
                for k in sorted(node)
                if k not in FINGERPRINT_EXCLUDED_KEYS
            )
            + "}"
        )
    if isinstance(node, list | tuple):
        return "[" + ",".join(_stable(x) for x in node) + "]"
    if isinstance(node, set):
        return "[" + ",".join(sorted(_stable(x) for x in node)) + "]"
    return json.dumps(node, sort_keys=True, ensure_ascii=False, default=str)


def template_fingerprint(
    profile: DocProfile,
    *,
    known_kinds: frozenset[str] | Iterable[str] = (),
    known_roles: frozenset[str] | Iterable[str] = (),
    schema_fields: Iterable[str] = (),
) -> str:
    """档案 + 它引用的词表 + 表字段白名单的指纹 (16 位)。

    刻意**不含** ``test_methods.yaml``: 方法库描述「怎么补齐」, 不是「怎么解析」——
    补齐方法的增删不该让「这份文档按什么模板解析」的指纹变化。两者混进一个哈希,
    之后每次补一条业界方法都要重采全部基线。

    只哈希结构不哈希文案: 见 :data:`FINGERPRINT_EXCLUDED_KEYS` 的理由。
    """
    payload = {
        "profile": {
            k: getattr(profile, k) for k in FINGERPRINT_PROFILE_KEYS if hasattr(profile, k)
        },
        "kinds": sorted(known_kinds),
        "roles": sorted(known_roles),
        "schema_fields": sorted(schema_fields),
    }
    canon = _stable(payload)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def template_identity(
    profile: DocProfile,
    *,
    known_kinds: Iterable[str] = (),
    known_roles: Iterable[str] = (),
    schema_fields: Iterable[str] = (),
) -> dict[str, str]:
    """落库用的模板身份三元组: (template_id, template_version, fingerprint)。

    ``template_id`` 缺失时**报错而不是回落到 profile 名**: 「模板身份」退化成
    「档案名」会让基线按档案分组, 而一个模板族可以有多个档案 —— 分组维度错了,
    后面所有按 (template_id, model_id) 存的基线都会串。宁可现在报错。
    """
    if not profile.template_id:
        raise ValueError(
            f"档案 {profile.name} 未声明 template_id —— 模板身份必须显式建模"
            "(方案 §4.0①)。请在 doc_profiles.yaml 里补 template_id/template_version; "
            "不要回落到用档案名代替, 一个模板族可以有多个档案。"
        )
    return {
        "template_id": profile.template_id,
        "template_version": profile.template_version,
        "fingerprint": template_fingerprint(
            profile,
            known_kinds=known_kinds,
            known_roles=known_roles,
            schema_fields=schema_fields,
        ),
    }
