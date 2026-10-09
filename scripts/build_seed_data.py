"""从方案 md 抽取「电源产品通用知识」-> Semantica 初始数据(一次性引导)。

    python scripts/build_seed_data.py
    python scripts/build_seed_data.py --spec <方案.md> --out data/seed

## 定位: 一次性引导, 不是常设管线

方案 md 是**引导源**。交付后不再依赖它 —— 后续补充知识直接编辑
``data/seed/power_domain_seed.json``, 不必回到方案, 也不必重跑本脚本。

本脚本保留只为**可复现**: 万一初始数据要从头重建, 能从同一份输入得到同一份
输出。它是自包含的(不 import 任何项目模块), 因为它服务于**架构之外**的引导 ——
架构内的知识维护是直接编辑 JSON。

## 输出形态

Semantica ``SeedDataManager.create_foundation_graph()`` 读 ``entities`` /
``relationships``; 实体取 ``id`` / ``name`` / ``type`` / ``text`` / ``properties``,
关系取 ``source`` / ``target`` / ``type`` / ``properties``。

**一份文件装全部**, 不按类型切碎 —— 关系要跨类型连边(拓扑连公式、公式连公理、
公式连勘误), 切开就断了。下游按 ``text`` 字段做图谱抽取, 所以 ``text`` 必须是
**自然语言陈述**, 而不是符号串。

## 审计形态

逐属性挂出处, 对齐 ``semantica.provenance.schemas.PropertySource`` /
``SourceReference``: 每个属性值带自己的 ``sources``, 而不是整个实体挂一个 ——
同一实体的不同字段可能来源不同(符号的量纲来自 U.5, 中文名来自术语表)。

``confidence`` 只给**查证过**的来源。方案原文未查证 -> ``None``, 不给 1.0:
「来自方案」不是「已验证」。给 1.0 会让所有未验证知识在审计里显得与已验证等价。

## SHACL 兼容

``properties.source_kind`` 区分 ``section``(方案章节号)与 ``standard``(真标准号)。
两者混同会让下游把章节号当认证依据(§18.10 注 8), 所以它是**受约束字段**,
不是自由文本。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

#: 归一化基准。满载自身比例恒为 1.0, 任何 xx%载 都相对它折算。
#: 「50%载」的 50% 是**相对满载**的, 不是绝对输出功率的百分数 —— 这条弄反了
#: 所有代入都会错, 而算式照样成立。
LOAD_CONDITIONS: tuple[dict[str, Any], ...] = (
    {
        "zh": "满载",
        "en": "full load",
        "ratio": 1.0,
        "kind": "load",
        "note": "归一化基准。任何 xx%载 都相对它折算。",
    },
    {
        "zh": "半载",
        "en": "half load",
        "ratio": 0.5,
        "kind": "load",
        "note": "= 50%载 = 满载 × 50%。",
    },
    {
        "zh": "空载",
        "en": "no load",
        "ratio": 0.0,
        "kind": "load",
        "note": "输出为 0。「空载」不等于「轻载」: 空载下常降频, 损耗模型不适用同一套参数。",
    },
    {
        "zh": "最小载",
        "en": "minimum load",
        "ratio": None,
        "kind": "load",
        "note": "比例待确认: 最小稳定负载由具体拓扑决定, 不是常数。收录但不给比例 —— "
        "编一个数会让规则在错误工况上运行且不报错。",
    },
    {
        "zh": "额定",
        "en": "rated",
        "ratio": None,
        "kind": "rating",
        "note": "保证工作点/保证极限。与「标称」不同, 不可互换。不给比例。",
    },
    {
        "zh": "标称",
        "en": "nominal",
        "ratio": None,
        "kind": "nominal",
        "note": "常规取整的代表值, 无保证含义。不给比例。",
    },
    {
        "zh": "最大额定",
        "en": "absolute maximum rating",
        "ratio": None,
        "kind": "rating",
        "note": "超过即可能损坏的绝对上限, 与「额定工作值」不是一回事。",
    },
    {
        "zh": "xx%载",
        "en": "fraction load",
        "ratio": None,
        "kind": "load",
        "pattern": r"^(\d+(?:\.\d+)?)\s*%\s*载$",
        "note": "**按模式求值, 不是固定值**: xx%载 = 满载 × xx%。例 30%载 -> 0.30。"
        "ratio 刻意为 null —— 它是待求量, 不是常量; 给出 1.0 之类的值会让推理"
        "把任意百分比都当成满载。整串锚定以免把 P_load 的 load 误认成工况词。",
    },
)
#: 工况别名词典: 别名 -> 规范名。**别名词也要能被规则匹配到** —— 规格书里写
#: 「50% 载」而知识库写「半载」时, 两者必须归一到同一个比例, 否则产测用例会漏掉
#: 一种工况写法。
LOAD_ALIASES: tuple[dict[str, Any], ...] = (
    {"alias": "50pct_load", "canonical": "half_load", "zh": "50%载"},
)

#: 工况 / xx%载 比例的推导, 编码成 Datalog(Horn 子句)。
#:
#: **为什么必须是规则而不是查表**: 查表只能回答「半载是几倍」, 规则才能回答
#: 「某型号 30% 载时输出功率的基准与比例是多少」—— 后者要靠具体型号的规格数据
#: 参与推理, 表里没有那些数据。
#:
#: 三条 Semantica 契约, 都是实测撞出来的, **每一条都会静默失效**:
#:
#: 1. **变量必须首字母大写**。``DatalogReasoner._is_variable`` 判 ``term[0].isupper()``,
#:    早先写 ``?ratio`` 会**被当成常量** —— 规则语法校验通过, 却永远匹配不上,
#:    推导结果为空且不报错。
#: 2. **同一变量名只能表一件事**。早先规则里 ``Base`` 同时当「满载比例」和
#:    「满载功率值」用, 合一失败, 同样静默推不出东西。
#: 3. **引擎是纯合一, 没有算术**。``multiply`` / 内建函数 / 聚合都不存在, 所以
#:    「30% 载 = 满载 x 0.3」这个乘法**在引擎里算不出来**。规则只推出
#:    (量, 工况, 满载基准值, 比例) 四元组, 乘法交给消费方 —— 那样乘法步骤本身
#:    才是可审计的, 而不是一个藏在引擎里、无法逐步复核的中间值。
#:
#: ``rule_str`` 是给 Semantica 直接消费的形态: ``add_rule()`` 只收字符串。
LOAD_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule_id": "load-alias",
        "comment": "工况别名归一: 50%载 = 半载。两者都要能匹配到同一比例。",
        "rule_str": "load_ratio(Alias, Ratio) :- load_alias(Alias, Canonical), load_ratio(Canonical, Ratio).",
        "head_predicate": "load_ratio",
        "head_args": ("Alias", "Ratio"),
        "body": [
            {"predicate": "load_alias", "args": ("Alias", "Canonical")},
            {"predicate": "load_ratio", "args": ("Canonical", "Ratio")},
        ],
    },
    {
        "rule_id": "load-scaling",
        "comment": (
            "某工况下某量的**满载基准值与载比例**。引擎无算术, 乘法由消费方做: "
            "value = FullLoadValue x Ratio。半载 = 50%载 = 满载 x 50%; "
            "xx%载 = 满载 x xx%。"
        ),
        "rule_str": (
            "value_at_load(Quantity, Load, FullLoadValue, Ratio) :- "
            "load_ratio(Load, Ratio), value_at_full_load(Quantity, FullLoadValue)."
        ),
        "head_predicate": "value_at_load",
        "head_args": ("Quantity", "Load", "FullLoadValue", "Ratio"),
        "body": [
            {"predicate": "load_ratio", "args": ("Load", "Ratio")},
            {"predicate": "value_at_full_load", "args": ("Quantity", "FullLoadValue")},
        ],
    },
)


def _load_key(en: str) -> str:
    """英文工况名 -> Datalog 常量名(下划线形态)。

    **规则匹配只认这个形态**。实测: 事实里若写 ``half load``(带空格), 规则
    ``load_ratio(Load, Ratio)`` 永远匹配不上 ``load_ratio(half_load, 0.5)``,
    且不报错。实体 id、实体 ``load`` 属性、事实三处必须走同一个函数。
    """
    return en.replace(" ", "_")


def build_facts() -> list[dict[str, Any]]:
    """Datalog 事实 —— **必须显式随数据发布**。

    ``DatalogReasoner.load_from_graph`` 读图时只产**一元**事实(``type``+``id``),
    产不出 ``load_ratio(full_load, 1.0)`` 这种二元事实。早先只发布规则不发布
    事实, 规则集因此是**死的**: 语法校验全过、``add_rule()`` 不报错, 而
    ``derive_all()`` 永远为空 —— 与「实体关系分两个键写」同一类静默失效。

    ``fact_str`` 是给 ``add_fact()`` 直接消费的形态, 三条实测契约:

    1. ``add_fact`` 的 **dict 分支只认** ``subject``/``predicate``/``object``。
       喂 ``{"predicate": ..., "args": [...]}`` 会走到
       ``logger.warning("Unrecognised dict fact format")`` 之后 **直接 return**
       —— 又一条不报错的丢失路径。``predicate``/``args`` 只作可读与审计用。
    2. **常量首字母大写会被拒绝** (``Facts must be constants only``)。规则里
       首字母大写才是变量, 事实里必须全小写。
    3. 字符串分支**不做小写化**(只有 dict 分支 lowercases), 空格与大小写都得
       自己写对。

    只发布与型号无关的通用事实。``load-scaling`` 规则要的
    ``value_at_full_load(...)`` 是**型号数据**, 不在这里编 —— 读完具体规格书
    后由消费方补进去; 凭空给一个功率值会让推理在错误型号上运行且不报错。
    """
    facts: list[dict[str, Any]] = []
    for item in LOAD_CONDITIONS:
        if item["ratio"] is None:
            continue
        key = _load_key(item["en"])
        facts.append(
            {
                "fact_id": f"load-ratio-{key}",
                "predicate": "load_ratio",
                "args": [key, str(item["ratio"])],
                "fact_str": f"load_ratio({key}, {item['ratio']})",
                "comment": (
                    "归一化基准, 比例恒为 1.0"
                    if item["ratio"] == 1.0
                    else f"{item['zh']} = 满载 x {item['ratio']:.0%}"
                ),
                "authority_kind": "project_defined",
            }
        )
    for alias in LOAD_ALIASES:
        facts.append(
            {
                "fact_id": f"load-alias-{alias['alias']}",
                "predicate": "load_alias",
                "args": [alias["alias"], alias["canonical"]],
                "fact_str": f"load_alias({alias['alias']}, {alias['canonical']})",
                "comment": f"{alias['zh']} 归一到 {alias['canonical']}",
                "authority_kind": "project_defined",
            }
        )
    return facts


#: 从 authority_ref 里抽标准号。
#:
#: **口径必须与 ``knowledge_gate.STANDARD_ID_HEAD`` 逐字一致** —— 它决定
#: ``defined_by`` 边建不建得出来, 那个决定「引用 nonexistent」报不报得出来。
#: 两者不一致时会得出**相反**的结论而**都不报错**: 这里默默少建一条边, 门禁默默
#: 放过一条悬空引用。``tests/test_gate_std_id_head.py`` 对真实种子的每个
#: ``authority_ref`` 断言两者给出同一个 head, 防的就是这个漂。
#:
#: 早先的写法有两个实测问题: 前缀只枚举了 GB/I、GB/T、GB/Z、GB、YY/T、IEC、
#: YD/T、JB/T(**漏掉** DL/T、GJB/Z、EN、IEEE、ISO、UL、CISPR —— 种子里都有),
#: 且 ``\d+(?:\.\d+)*`` 只认点分段(``CISPR 16-1-2`` 会截成 ``CISPR 16``)。
#: 现在用通用形态, 没见过的新族也能被提取 —— 漏检是静默的, 误提取会报出来。
#:
#: **必须用 :meth:`re.Pattern.match` 而不是 ``search``** —— ``authority_ref``
#: 一律**以标准号开头**(``"GB/T 2900.70-2008 §442-01-01"``、``"GB/T 3187-1994 7.2.8;
#: 现行 …"``), 所以锚定在开头既够用又安全。
#:
#: 早先用 ``search`` 时踩过两次: ``std::GB/T 17626.2-2018`` 的 ``authority_ref``
#: 被 corrections 填成了查询网址 ``https://std.samr.gov.cn/…/gbDetailed?id=71F7…``,
#: ``search`` 在网址里匹配到十六进制片段 ``71F772D`` 当成了标准号 —— 门禁据此报出
#: 「71F772D 引用了不存在的标准」这类**假错误**。锚定后一律提取不到, 正确跳过。
#:
#: **部分号允许一位数字**: ``DL/T 5-2019``(电力可靠性管理规定)是真实标准。
#: 不用「至少两位」去规避十六进制误匹配 —— 那是拿真实数据迁就正则。
#:
#: **末尾裸 ``+`` 允许**: ``IEC 61850-2013+``(含后续修正件)在种子里就是这么写的。
#:
#: 修正件后缀 ``+A2:2013`` 的字母**后面还有数字**(修正件号), 所以是
#: ``[A-Za-z]\d*``; 早先写成 ``[A-Za-z]`` 会把它截成 ``+A``, 与库里实体对不上。
#:
#: **IEC 式年份用冒号** —— ``IEC 60664-1:2020``, 这是 IEC 的规范写法(不是笔误)。
#: 早先的正则只认 ``-2018`` 那类短横线年份, 于是从 ``IEC 60664-1:2020`` 里抽出
#: ``IEC 60664-1`` —— 而库里实体是 ``std::IEC 60664-1:2020``, 抽出来的 head 与实体 id
#: 对不上, 门禁报「引用指向库里不存在的标准实体」。这是**工具侧的缺口**, 表现却像数据错:
#: 生成器默默少建一条 ``defined_by`` 边, 门禁默默多报一条假悬空引用, 两边都不报错。
#: 所以补 ``(?::\d{4})?``。注意它与上面 ``+A2:2013`` 的冒号是两回事: 那个冒号跟在修正件号
#: 后面, 这个跟在标准号本体后面, 两者位置不同, 互不干扰。
_STD_ID_RE = re.compile(
    r"[A-Z]{2,6}(?:/[A-Z]{1,6})?\s*\d+[A-Za-z]?(?:[-.]\d+[A-Za-z]?)*"
    r"(?::\d{4})?"
    r"(?:\+(?:[A-Za-z]\d*(?::\d{4})?)?)?"
)

_BOOTSTRAP = "V6.0 开发指导方案"


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _clean(text: str | None) -> str | None:
    """去 markdown 装饰: 反引号、星号、尾随的 ✅ 与标点。"""
    if text is None:
        return None
    out = text.replace("`", "").replace("**", "").strip()
    out = out.strip(" ✅✓*")
    return out or None


#: **权威依据**的类型。这是决定「这条数据能不能当依据」的唯一字段。
#:
#: - ``standard``        标准依据 —— ``authority_ref`` 给标准号(+条号), 可作认证依据
#: - ``book``            具名出版物依据: 专著/手册/教材。有书名、版次、出版社才算
#:                       依据; 「业内一般认为」不是
#: - ``industry``        业界依据但非出版物: 行业术语表、主流厂商规格书、通行叫法
#: - ``project_defined`` **已确认无外部对应术语**, 本项目自行定义
#: - ``unverified``      **尚未查证** —— 这是待办, 不是结论
#:
#: ``unverified`` 与 ``project_defined`` 必须可区分: 前者要继续查, 后者是查完
#: 的结论。混起来等于把待办显示成已完成。
AUTHORITY_KINDS = ("standard", "book", "industry", "project_defined", "unverified")

#: ``produced_by`` 的取值 —— **构建期**信息, 决定权威类型的默认值。**不写进记录**。
#:
#: 方案 md 只是**设计思路与参考数据**: 它告诉我们「这个领域有哪些概念」, 但
#: **不能作为这些概念的出处**。早先一版把 ``source_ref=V6.0§X`` 当出处, 等于
#: 引用了一份本身不是依据的文件 —— 看起来可追溯, 实则无法复核。
#:
#: - ``bootstrap_section``  从方案 md 实读到, 行号可回溯(**仅线索**)
#: - ``derived_from_id``    由实体 ID 编码反推(公式 ``F_J.2.1`` -> ``J.2.1``)。
#:                         **连线索都不算** —— ID 编码可能与实际章节不一致
#: - ``correction``         来自 corrections.yaml(每条自带外部权威出处)
#: - ``convention``         本项目约定(工况词口径)。方案里没有这个概念的出处
#: - ``standard``           真标准号
#: - ``industry_practice``  **业界通行做法**(websearch 查证的多个来源一致)。它既不是
#:                         本项目约定(别人也这么做), 也不是可引的标准条款(纹波测法在
#:                         TI 应用笔记、老化时长在各厂商实践里), 所以单列一类而不是
#:                         塞进 ``standard``—— 塞进去会让「出处必须可查」变成空话:
#:                         读者去翻那个标准号却找不到对应条款。默认权威级
#:                         ``industry``, 但产测工艺条目仍显式给 ``unverified``
#:                         (见 :func:`build_production_practice` 的理由)。
PRODUCED_BY = (
    "bootstrap_section",
    "derived_from_id",
    "correction",
    "convention",
    "standard",
    "industry_practice",
)

#: 线索类型 -> 默认权威类型。**只有 ``standard`` 与 ``convention`` 能直接推断**:
#: 从方案读到的概念一律 ``unverified`` —— 读到不等于有依据。
_PRODUCED_BY_TO_AUTHORITY = {
    "bootstrap_section": "unverified",
    "derived_from_id": "unverified",
    "correction": "standard",
    "convention": "project_defined",
    "standard": "standard",
    # 业界做法**不是**依据本身: 多个厂商实践一致只说明「通行」, 不构成可复核
    # 的出处。给 unverified(而非 industry)是为了让它与「有标准号但未查证」在
    # 检索结果里可区分 —— 前者缺的是条款号, 后者缺的是整个来源。
    "industry_practice": "unverified",
}


def authority_ref(
    ref: str | None,
    *,
    kind: str,
    line_no: int | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    """构造 ``semantica.provenance.schemas.SourceReference`` 形态的**权威出处**。

    ``kind`` 取 :data:`AUTHORITY_KINDS` 之一。受约束字段: 它决定下游能不能把
    这个出处当认证依据。

    ``confidence`` 为 ``None`` 表示**未查证**。「方案里提到过」不是「已验证」,
    给 1.0 会让未查证内容在审计里与已查证等价。

    ``ref`` 为 ``None`` 且 ``kind`` 非 ``standard`` / ``book`` 时表示「**没有**
    权威出处」, 而不是「出处未知」。后者危险得多 —— 前者是事实, 后者是没查。
    """
    if kind not in AUTHORITY_KINDS:
        raise ValueError(f"authority_kind 必须是 {AUTHORITY_KINDS} 之一, 得到 {kind!r}")
    if kind in ("standard", "book") and not ref:
        raise ValueError(f"authority_kind={kind} 时必须给 authority_ref(标准号/书名)")
    return {
        "document": ref or "",
        "section": ref,
        "line": line_no,
        "confidence": confidence,
        "metadata": {"authority_kind": kind},
    }


def _props(
    values: dict[str, Any],
    produced_by: str,
    line_no: int | None,
    *,
    authority_kind: str | None = None,
    authority: str | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    """组装 properties + 逐属性 provenance。

    **只持久化权威依据, 不持久化线索来源。**

    方案 md 是设计思路与参考数据 —— 它告诉我们这个领域有哪些概念, 但**不构成
    这些概念的依据**。而且它在 ``.gitignore`` 里, 所以 ``V6.0§6.3.1`` 这种引用
    任何人都解析不了: 留着一个查不到的引用, 正是「看起来可追溯、实则无法复核」
    的那种假出处, 比不写更坏。整份数据的引导源只在 ``provenance.bootstrap_source``
    里记一次文件名即可。

    ``produced_by`` 是**构建期**参数(见 :data:`PRODUCED_BY`), 只用来在没显式给
    ``authority_kind`` 时推断默认权威类型, **不写进记录**。从方案读到的一律
    ``unverified`` —— 读到不等于有依据。

    只给**有值**的属性挂 provenance —— 给空值挂一条出处是在声称「这个空值也有
    来源」, 那会让冲突检测把「未提供」误判成「两处来源不一致」。
    """
    if produced_by not in PRODUCED_BY:
        raise ValueError(f"produced_by 必须是 {PRODUCED_BY} 之一, 得到 {produced_by!r}")
    kind = authority_kind or _PRODUCED_BY_TO_AUTHORITY[produced_by]
    ref = authority_ref(authority, kind=kind, confidence=confidence)
    out = {k: v for k, v in values.items() if v is not None}
    out["authority_kind"] = kind
    if authority:
        out["authority_ref"] = authority
    # 每条值都挂**权威出处**。``authority_kind=unverified`` 时 confidence 为
    # None, 语义是「未查证」而不是「查了没有结果」。
    out["provenance"] = {
        k: {"property_name": k, "value": v, "sources": [ref]}
        for k, v in out.items()
        if k not in ("authority_kind", "authority_ref", "provenance")
    }
    # **confidence 必须落进 properties, 否则查证结论到不了记录级。**
    #
    # ``to_seed_records`` 的记录级 ``confidence`` 读的是 ``props.get("confidence")``,
    # 而 ``confidence`` 此前**只**被传给 :func:`authority_ref` 落进 provenance 的
    # SourceReference —— 从没进过 properties。于是走 :func:`_props` 直接产出的实体
    # (抽取器建的标准/概念/符号/工况) 无论查证到多高可信度, 记录级 confidence
    # **恒为 None**。
    #
    # 实测丢失 106 条已验证知识的 confidence: 81 power_concept / 14 standard /
    # 6 load_condition / 3 load_ratio / 2 symbol, 其中 **67 条 authority_kind=standard**
    # (含 ``YX_*`` 保护概念、GB/T 2900.x 全系列、GB 4943.1-2022)。
    #
    # 只在非 None 时写: None 的语义是「未查证」, 而 :func:`authority_ref` 已要求
    # standard/book 必须给 ref, 两者不得混同。放在 provenance 之后 —— 写早了会让
    # ``provenance["confidence"]`` 多出一条指向自身的条目。
    if confidence is not None:
        out["confidence"] = confidence
    return out


# ---------------------------------------------------------------------------
# 各类知识
# ---------------------------------------------------------------------------


#: 概念字典表头(§6.3.1): | concept_id | pref_label_zh | pref_label_en | aliases | qudt_ref |
#:
#: **必须按表头定位表格。** 早先一版用「行首是大写 ID」全文匹配, 结果把方案里
#: 其它表格的术语全抓进来 —— 实测产出 232 条「电源概念」, 其中含
#: ``Apache Graph Extension`` / ``Row-Level Security`` /
#: ``Hierarchical Navigable Small World`` / ``Shapes Constraint Language``。
#: 那是**本项目的实现细节**, 不是电源产品知识; 混进领域知识后, Semantica 会
#: 拿「HNSW 是一个可测量」去推理, 而且不报错。真正的概念字典是**带 qudt_ref
#: 的那 23 条**(输出纹波 / 额定输出电压 / 整机效率 / 功率因数 / 过温保护 …)。
_CONCEPT_HEADER = re.compile(
    r"^\|\s*concept_id\s*\|\s*pref_label_zh\s*\|\s*pref_label_en\s*\|\s*aliases\s*\|"
)


#: ``aliases`` 列内部的别名分隔符。**不含 ``/``** —— 实测存在别名本身带斜杠
#: (``电快速瞬变, EFT/B``), 切斜杠会把一个别名劈成两个不存在的别名, 而检索时
#: 两个都命中不了真正那个词。
_ALIAS_SPLIT = re.compile(r"[,、;；]")


def apply_concept_fixes(entities: list[dict[str, Any]]) -> list[str[str]]:
    """改名 + 补别名 (:data:`CONCEPT_RENAMES` / :data:`CONCEPT_ALIAS_PATCH`)。

    改名**不在此处改引用**: 实测被改名的概念零引用, 所以不需要同步; 万一将来有
    引用了而这里还照改, 就会造出悬空引用 —— 所以改名清单必须人工复核引用面,
    引用面非零时要一并改 :data:`CONCEPT_RENAMES` 的用法, 而不是靠代码兜。

    顺带把改名后的旧名记进 ``dropped_synonyms``/``renamed_from``, 让审计能
    回答「原来那个 id 去哪了」—— 否则改名就成了无法追溯的静默变更。
    """
    by_id = {e["id"]: e for e in entities}
    log: list[str] = []
    for old_id, new_id in CONCEPT_RENAMES.items():
        e = by_id.get(old_id)
        if e is None:
            log.append(f"{old_id}: 不存在, 跳过改名")
            continue
        props = e.setdefault("properties", {})
        props["renamed_from"] = old_id
        # 同义词里补上新 id 的英文写法 —— 否则改名后按新 id 搜不到英文别名
        e["id"] = new_id
        log.append(f"{old_id} -> {new_id}")
        by_id[new_id] = e
        del by_id[old_id]
    for cid, patch in CONCEPT_ALIAS_PATCH.items():
        e = by_id.get(cid)
        if e is None:
            log.append(f"{cid}: 不存在, 跳过别名补丁")
            continue
        props = e.setdefault("properties", {})
        syn = list(props.get("synonyms") or [])
        for s in patch.get("synonyms", []):
            if s not in syn:
                syn.append(s)
        props["synonyms"] = syn
        if patch.get("note"):
            props["note"] = patch["note"]
        log.append(f"{cid}: 别名补到 {len(syn)} 条")
    return log


def build_authoritative_terms() -> list[dict[str, Any]]:
    """:data:`AUTHORITATIVE_TERMS` -> ``power_concept`` 实体。

    这些是**抽象层级**的术语, 方案 md 的概念字典里没有 —— 字典只有具体量纲
    (额定容量/标称容量/额定输出电压…), 所以「额定」与「标称」的区分此前只
    发生在具体量纲上, 抽象层是空的, 于是每个具体量纲各自决定要不要区分,
    结果就是有的区分了(容量)、有的互为同义词(电压)。

    ``type`` 用 ``power_concept`` 而不是新类型: 它们是概念, 与其它概念在同
    一命名空间, 检索与推理无需区分。新增类型会让「概念」这个语义分裂成两半。
    """
    out: list[dict[str, Any]] = []
    for spec in AUTHORITATIVE_TERMS:
        # ``verbatim=False`` 时把释义来源标出来。查证深度有深浅两种(条款号出现在
        # 术语清单里 vs 定义正文逐字抄到), 不区分的话读者会以为每条都核对过原文。
        verbatim = spec.get("verbatim", True)
        definition = (
            spec["definition"]
            if verbatim
            else (
                f"{spec['definition']} "
                f"[释义由本项目撰写 —— 条款号已核对 {spec['standard']} "
                f"§{spec['standard_section']} 术语清单, 定义正文未逐字核对]"
            )
        )
        # 释义逐字抄录的置信度高; 本项目撰写的低一档 —— 两者的可复核性不同。
        confidence = 0.95 if verbatim else 0.85
        out.append(
            {
                "id": spec["id"],
                "name": spec["name"],
                "type": "power_concept",
                "text": f"{spec['id']}: {spec['name']} {spec['en']} — {definition}",
                "source": f"{spec['standard']} §{spec['standard_section']}",
                "properties": _props(
                    {
                        "zh": spec["name"],
                        "en": spec["en"],
                        "synonyms": list(spec["synonyms"]),
                        "definition": definition,
                        "definition_verbatim": verbatim,
                        "ie_ref": spec.get("ie_ref"),
                        "defined_by_standard": spec["standard"],
                        "standard_section": spec["standard_section"],
                        "note": spec.get("note"),
                    },
                    "standard",
                    0,
                    # authority_ref 必须给到**条款号**: 只有标准号时, 读者仍要自己
                    # 去翻几百页找哪一条, 而「哪一条」正是本条术语与相邻术语的
                    # 全部区别所在(442-01-01 vs 442-01-04)。
                    authority=f"{spec['standard']} §{spec['standard_section']}",
                    confidence=confidence,
                ),
            }
        )
    return out


#: **产测工艺(measurement practice)条目**(2026-10-07 websearch 查证)
#:
#: 起因(实测): PA601 规格书 182 条判据里, 124 条属于产测/安规范围, 但
#: ``power_concept`` 里**没有任何一条**讲「怎么测」的知识 —— 只有被测的量
#: (``VOUT_RIPPLE``/``EFFICIENCY``/``OCP_PROTECT``…)。「20MHz 限带」「并接
#: 10uF+0.1uF 电容」「负载突变速率 0.1A/uS」这些条件只以自由文本躺在规格书备注里,
#: 且多数型号根本没有。而这些恰恰是**换一个测法就得到不同数字**的地方。
#:
#: ## 为什么属于域(电源)知识而不是型号知识
#:
#: 产测工艺是**跨型号共用**的: 任何 -48V/-54V 通信整流器都要按 20MHz 限带测纹波、
#: 都要四线制采样测效率。PA601 的规格书写了这些条件, 换个型号可能不写, 但**做法
#: 不变**。所以进 ``power_concept``(域库), 不进 ``pw_<model>``(型号库)——
#: 放型号库会导致「这套测法只在 PA601 上有记录」, 而实际每个型号都要用。
#:
#: ## 每条都要能回答「不这么做会测错成什么样」
#:
#: 只写「要用 20MHz 限带」没有信息量(读的人不知道限带是测还是不测)。每条都必须
#: 给出**错误做法会导致的具体偏差**, 这也是 websearch 查到的业界共识。
#:
#: **权威级一律 ``unverified``**: 这些做法业界通行, 但本项目**没有**标准条款号
#: 可引(纹波测法在 TI 应用笔记里, 老化时长在各厂商实践里, 都不构成可引的标准)。
#: 按红线 5 的纪律, 拿不到条款号就不标 ``standard`` —— 标了会让「出处可查」这条
#: 变成空话。它们的价值在于「工艺清单完整」, 不在于「可追溯到某条款」。
PRODUCTION_PRACTICE: tuple[dict[str, Any], ...] = (
    {
        "id": "MEAS_RIPPLE_BW_LIMIT",
        "practice_scope": "condition",
        "name": "纹波测试带宽限制",
        "en": "ripple measurement bandwidth limit",
        "practice": (
            "测输出纹波时示波器必须开 20MHz 带宽限制, 且探头置 1X 无衰减档、"
            "交流耦合、接地弹簧/短地针(接地线长于 1cm 即失效)。"
        ),
        "why_wrong_without_it": (
            "不限制带宽会把开关节点辐射与共模电流经长地线拾取进来。业界实测: "
            "同一电路, 错误测法读到上百 mV 乃至数百 mV 的「纹波」, 而正确测法"
            "为十几 mV —— **误差可达 10 倍以上**, 足以把合格品判成不合格。"
        ),
        "spec_example": (
            "PA601 SR-1206 峰峰值杂音电压 -54V 轨 ≤500mVpp, 备注明确「20MHz 带宽"
            "限制, 并接 10uF 电解电容和 0.1uF 电容测试」—— 说明客户也按这个测。"
        ),
        "tags": ["纹波", "测量方法"],
    },
    {
        "id": "MEAS_RIPPLE_DECOUPLING_CAP",
        "practice_scope": "condition",
        "name": "纹波测试并联电容",
        "en": "ripple test decoupling capacitor",
        "practice": ("在输出测点并接 10uF 电解电容 + 0.1uF 陶瓷电容(高频噪声旁路)后测纹波。"),
        "why_wrong_without_it": (
            "模块输出端的二次纹波与开关噪声在空载/轻载时最坏; 不加旁路电容测到的"
            "是「模块自身纹波 + 探头拾取」之和, 而客户系统里实际存在的是加了电容后"
            "的值。不加就测了个跟客户用不同的东西。"
        ),
        "spec_example": "PA601 SR-1206 备注: 「并接10uF 电解电容和0.1uF 电容测试」。",
        "tags": ["纹波", "工装"],
    },
    {
        "id": "MEAS_EFFICIENCY_KELVIN_4WIRE",
        "practice_scope": "condition",
        "name": "效率测试四线制采样",
        "en": "four-wire Kelvin sampling for efficiency",
        "practice": (
            "测整机效率/输出电压时用四线制(Kelvin): 载流线走大电流, 检测线(仅过"
            "测量电流, 高阻)直达 DUT 引脚, 两者物理分离; 电子负载开远程采样。"
        ),
        "why_wrong_without_it": (
            "两线法把载流线的电阻压降算进被测量。行业资料给出的量级: 引线电阻 1~10mΩ, "
            "在 600W/54V(11.1A)下 10mΩ 就是 **111mV**, 占额定输出的 0.2% —— 而"
            "效率判据本身只有 86~93% 量级(1% 即 0.86 个百分点)。老化房实测同样问题:"
            "老化架距电源 20m、20A、导线 0.02Ω, 压降 0.4V, 对 12V 供电件是 3% 以上,"
            "「电源显示值」与「负载端实际值」差一大截, 工位之间工况就不公平了。"
        ),
        "spec_example": (
            "PA601 SR-1210 整机效率 20%/50%/满载三档(86/91/93%), 备注「额定220Vac "
            "输入」—— **没写采样接法**。这是规格书的缺口, 不是可省的动作。"
        ),
        "tags": ["效率", "四线制", "工装"],
    },
    {
        "id": "MEAS_DYNAMIC_LOAD_SLEW",
        "practice_scope": "condition",
        "name": "动态响应负载跳变",
        "en": "dynamic load step for transient response",
        "practice": (
            "动态响应按标准负载阶跃测: 25%→50%→25% 或 50%→75%→50%, 并记录负载"
            "突变速率; 恢复时间按**超出输出电压范围的部分**计算, 不是等回到稳态。"
        ),
        "why_wrong_without_it": (
            "**PA601 SR-1211 这条判据按字面执行会失效**: 备注写「负载突变速率"
            "≤0.1A/uS」, 而 25%→50% 是 2.775A 台阶(54V 轨 11.1A), 按 0.1A/µs 算"
            "斜坡需 **27.75ms**, 而被测的恢复时间是 **200µs** —— 激励比被测量慢 139 倍。"
            "控制环带宽在 kHz 量级, 会全程跟随这个缓斜坡, 「恢复时间 ≤200µs」平凡通过,"
            "测不出环路动态。业界做法是 10~20A/µs(对应 0.14~0.28ms 斜坡)。"
            "**原文的 ≤ 需向中兴确认是否为 ≥ 的笔误。**"
        ),
        "spec_example": "PA601 SR-1211 动态响应恢复时间 -54V 轨 ≤200µs, 3.45V 轨 ≤5µs。",
        "tags": ["动态响应", "电子负载"],
    },
    {
        "id": "MEAS_PROTECT_SWEEP_LOAD",
        "practice_scope": "condition",
        "name": "保护动作值扫描",
        "en": "protection trip point sweep",
        "practice": (
            "过流/过压保护动作点用**扫描法**测: 电子负载 CC 模式逐步加流(或可编程源"
            "逐步升压)直到保护动作, 记录动作点。带容性负载的项用 **CR 模式**。"
        ),
        "why_wrong_without_it": (
            "固定负载点测保护只能验「在这一档动不作动」, 验不出动作点落在区间内哪"
            "里 —— 而区间(如 OCP 12~18A)的两端才是设计余量所在。"
        ),
        "spec_example": (
            "PA601 SR-1216 带容性负载 ≤2000uF 备注「采用 CR 模式进行测试」;"
            "SR-1309 过流保护 12~18A。"
        ),
        "tags": ["保护", "电子负载"],
    },
    {
        "id": "MEAS_HIPOT_ISOLATE_LOAD",
        "practice_scope": "condition",
        "name": "耐压测试断开负载与通信",
        "en": "disconnect load and comms before hipot",
        "practice": (
            "耐压/绝缘测试前必须断开: 电子负载、通信线、Y 电容相关回路; 接地阻抗"
            "测试需断开全部 PE 连接并使用浮地工装。"
        ),
        "why_wrong_without_it": (
            "负载与通信口的耐压等级低于被测回路, 不断开会在它们上击穿/漏电, "
            "读数被拉高甚至损坏接口; Y 电容直通对地会绕过被测绝缘路径, 使绝缘"
            "电阻读数完全失真。"
        ),
        "spec_example": (
            "PA601 SR-2500/2501/2502 绝缘电压 4000Vdc/2500Vdc/500Vdc 1min"
            "漏电流≤10mA —— 规格书**未写断开要求**。"
        ),
        "tags": ["安规", "工装"],
    },
    {
        "id": "PRACT_BURN_IN_GRADED",
        "practice_scope": "process",
        "name": "老化筛选(梯度工况)",
        "en": "burn-in screening with graded load",
        "practice": (
            "产线老化按**梯度**三段: 轻载约 20%(查待机功耗/轻载异常)、半载 50~60%"
            "(日常工作态)、满载 100%(虚焊与热衰减), CC 恒流模式, 时长行业普遍"
            "4~8h(高可靠 12h 以上, 加速筛选 24~48h), 老化后复测全套参数。"
        ),
        "why_wrong_without_it": (
            "只做满载会漏掉轻载抖动、低压不稳、待机异常; 而这些正是客户现场最先"
            "遇到的。行业数据: 老化可拦截约 99% 的工艺缺陷产品、约 90% 潜在故障"
            "在出厂前暴露。**不做老化的直接后果是早期失效率** —— 元件虚焊/器件"
            "漂移在通电头几十分钟到几十小时集中爆发, 那正是客户收到货的时段。"
        ),
        "spec_example": (
            "PA601 规格书**完全没有老化条款** —— SR-2200~2210 是环境/运输型式试验, "
            "不是产线老化。这是规格书未覆盖而产线必需的一环。"
        ),
        "tags": ["老化", "产线工艺"],
    },
    {
        "id": "PRACT_BURN_IN_4WIRE",
        "practice_scope": "process",
        "name": "老化供电电压一致性",
        "en": "burn-in supply voltage uniformity",
        "practice": (
            "多工位老化用集中供电时: 粗线走线为基 + 电源远程补偿校差 + 各工位到端"
            "电压监测留证; 电源限流按台数留余量并配工位级熔断隔离; 不同电压等级"
            "分组老化。"
        ),
        "why_wrong_without_it": (
            "批量老化时靠电源近的工位与远的工位电压不一致, **老化条件的公平性无从"
            "谈起** —— 等于不同产品过了不同的应力。并联母线上一台短路会独占电流"
            "拉低其余工位电压, 一台坏件毁掉整批老化的有效性。"
        ),
        "spec_example": "PA601 多路输出(-54V/3.45V)需分组或分路老化, 避免互相欠压。",
        "tags": ["老化", "工装", "供电"],
    },
    {
        "id": "PRACT_REGRESSION_AFTER_BURNIN",
        "practice_scope": "process",
        "name": "老化后复测",
        "en": "post burn-in retest",
        "practice": (
            "老化结束后复测输出电压、纹波、效率, 确认长时间工作后参数不衰减;"
            "老化全程监测输出电压与温度。"
        ),
        "why_wrong_without_it": (
            "只看老化中有没有报警, 测不出「参数漂移但仍在规格内」—— 而漂移趋势是"
            "批次性问题的早期信号。行业做法是对母线电流做趋势分析: 台阶式突变或"
            "持续爬升往往意味某台开始异常。"
        ),
        "spec_example": "PA601 SR-1206 纹波、SR-1210 效率需在老化后复测。",
        "tags": ["老化", "复测"],
    },
    {
        "id": "PRACT_SAMPLING_AQL",
        "practice_scope": "process",
        "name": "抽样检验方案",
        "en": "AQL sampling plan",
        "practice": (
            "100% 电性全检 + 按 GB/T 2828.1 的 AQL 抽样做可靠性/型式试验。行业参考值:"
            "电性类不良 AQL 0.65、外观 2.5; 可靠性抽样比例首批 1~5%、正常批次 "
            "0.1~1%。"
        ),
        "why_wrong_without_it": (
            "不区分「全检项」与「抽检项」会导致两种错: 把型式试验(EMC/环境/运输)当"
            "产线全检项 → 产线做不出来或节拍崩溃; 或者该抽检的项目全检 → 成本失控。"
            "PA601 182 条判据里 **39 条是型试、18 条是混合**, 只有 115 条属产测范围,"
            "这个区分必须显式。"
        ),
        "spec_example": (
            "PA601 SR-2506 放电管接地方式备注「系统生产线要求全部进行耐压测试…"
            "原则上系统生产时可以进行抽样测试」—— 客户自己就区分了全检/抽检。"
        ),
        "tags": ["抽样", "产线工艺"],
    },
    {
        "id": "PRACT_CALIBRATION_TRACEABILITY",
        "practice_scope": "process",
        "name": "量具校准与溯源",
        "en": "instrument calibration and traceability",
        "practice": (
            "测试脚本版本、**仪器校准状态**、环境温湿度等元数据须与测试数据一并"
            "入档; 校准到期需在产测前拦截。测量系统分析(MSA)给出平均次数 n 与"
            "保护带(guardband)。"
        ),
        "why_wrong_without_it": (
            "行业原话: 数据的价值取决于采集的规范性 —— 元数据不入档, 数据之间"
            "**可比性无从谈起**。没有 guardband 时, 判据贴着测量不确定度边缘, "
            "同一台产品不同时间测会给出不同结论。"
        ),
        "spec_example": (
            "PA601 全部判据**无保护带要求**, 也无校准状态字段 —— "
            "`pw_sr5400.instrument_ledger.cal_due` 字段存在但全表 0 行。"
        ),
        "tags": ["校准", "MSA", "数据"],
    },
    {
        "id": "PRACT_SOE_TIME_RESOLUTION",
        "practice_scope": "condition",
        "name": "保护动作时间测量",
        "en": "protection action time measurement",
        "practice": (
            "保护动作时间需专用 SOE/事件顺序记录测试, 时间分辨率应与被测时间同"
            "量级; 用通信口轮询采样测毫秒级动作时间是测不准的。"
        ),
        "why_wrong_without_it": (
            "SR-1213 开机输出延迟 ≤6s(常温)/≤12s(-25℃)、SR-1211 恢复时间 ≤200µs "
            "这类判据, 用秒级轮询根本看不到过程 —— 会把「没有动作」误判成"
            "「动作很慢」或反之。"
        ),
        "spec_example": (
            "PA601 SR-1213 开机输出延迟; 术语表已有 INSTR_SOE_TESTER(SOE测试仪)但无任何判据引用它。"
        ),
        "tags": ["时间", "SOE"],
    },
    {
        "id": "PRACT_POWER_STEP_VOLTAGE_BOUNDARY",
        "practice_scope": "condition",
        "name": "三边界电压扫描",
        "en": "three-boundary voltage sweep",
        "practice": (
            "EOL 用可编程交流源自动扫掠 低压/额定/高压 三个边界点, 脚本化执行,"
            "验证全输入范围内输出正常无异常保护。"
        ),
        "why_wrong_without_it": (
            "只在额定电压测一遍, 测不到输入边界处的异常: 欠压时输出跌落、"
            "过压时保护误动作、跨区间的动态不稳定。这些是客户现场最容易遇到的工况。"
        ),
        "spec_example": (
            "PA601 SR-1101 输入工作电压范围 88~290Vac、SR-1102 最高极限 315Vac"
            "—— 判据给了范围, 但**没有要求在范围内扫点**。"
        ),
        "tags": ["输入", "可编程源"],
    },
    # ---------------- 工装侧(2026-10-07 第二批 websearch) ----------------
    {
        "id": "FIXTURE_TEST_POINT_AND_ALIGNMENT",
        "practice_scope": "process",
        "name": "测点与对位防呆",
        "en": "test point and poka-yoke alignment",
        "practice": (
            "PCB 上为每个产测要测的信号留裸露测试点(test pad), 并留 2~3 个**非对称**"
            "定位孔; 治具用定位销对位 + 压盖压合 pogo pin, 压不下就放不进(物理防呆)。"
        ),
        "why_wrong_without_it": (
            "行业总结的失效模式: 放反/放偏还硬压 -> 戳坏板子或治具, 并测出一堆"
            "**假 FAIL**; 测试点太小太密 -> 针顶不准, 接触时灵时不灵。治具与 PCB"
            "设计必须**并行**: 板子画完才想起治具, 就已经没地方留测试点了。"
        ),
        "spec_example": (
            "PA601 SR-1200 备注「测试点为输出端子与PCB连接部」—— 测点位置有定义, "
            "但**无对位/防呆要求**, 而 PA601 是插拔安装的多模块结构(SR-0100)。"
        ),
        "tags": ["工装", "防呆", "测点"],
    },
    {
        "id": "FIXTURE_PROBE_LIFE_AND_RESISTANCE",
        "practice_scope": "process",
        "name": "探针寿命与接触电阻",
        "en": "probe life and contact resistance",
        "practice": (
            "治具探针(pogo pin)按压合次数管理寿命: 每日清洁氧化层、每 5000 次"
            "更换关键信号探针、每 20000 次做全通道接触电阻测量(应 <1Ω); 工装本身"
            "登记接触电阻/压降并定期校准。"
        ),
        "why_wrong_without_it": (
            "接触电阻随压合次数上升, 而它**直接进测量结果** —— 尤其配合四线制"
            "以外的接法, 接触电阻压降会被算成产品误差。且接触不良表现为时好时坏,"
            "不是稳定偏大, 极难从数据上察觉。"
        ),
        "spec_example": (
            "`pw_sr5400.fixture_tp_probe.life_hours` / `wear_level` 与 "
            "`fixture_channel_map.contact_res_mohm` 字段已存在但**全表 0 行** —— "
            "工装侧的寿命与接触电阻管理完全没有数据。"
        ),
        "tags": ["工装", "接触电阻", "维护"],
    },
    {
        "id": "FIXTURE_RELAY_MATRIX_ROUTING",
        "practice_scope": "process",
        "name": "继电器矩阵信号路由",
        "en": "relay matrix signal routing",
        "practice": (
            "用多通道复用继电器板/矩阵切换激励与测量通路: 负载通道切换、信号激励"
            "切换、短接点切换, 上位机经 RS485 控制任意一组继电器动作。"
        ),
        "why_wrong_without_it": (
            "继电器板是多工位共用的必备件 —— 没有它就得靠人工插拔换线, 而人工"
            "换线是**误操作与节拍的共同瓶颈**。也因为矩阵切换会引入回路, 需要"
            "「非测试状态下把未用工位高阻悬空」的处置, 否则工位间互相干扰。"
        ),
        "spec_example": (
            "PA601 多路输出(-54V/3.45V)、多工位需求(SR-1219 均流要求两台并机、"
            "SR-1309 测试方法「两台电源并机」)都依赖通路切换, 但知识层无此条目。"
        ),
        "tags": ["工装", "继电器", "切换"],
    },
    {
        "id": "FIXTURE_MULTI_STATION_ISOLATION",
        "practice_scope": "process",
        "name": "多工位隔离与接地",
        "en": "multi-station isolation and grounding",
        "practice": (
            "多工位并行测试时: 每工位独立隔离电源、信号线屏蔽双绞且**单端接地**、"
            "继电器矩阵把未用工位高阻悬空、上位机分时轮询仪器总线; 小信号工位"
            "间距建议 ≥25cm。"
        ),
        "why_wrong_without_it": (
            "行业列出的并行干扰成因: 共地引入地弹噪声、同时发起测量互相拉低、"
            "未用工位悬空引入耦合。表现为**随机 FAIL 且难复现** —— 数据上看是产品"
            "不良, 实际是测试系统自身的问题。"
        ),
        "spec_example": (
            "PA601 SR-1110 接地方式明确「输出信号地要与 -54VRTN 完全隔离」, "
            "工装必须能实现这个隔离, 否则测不出真实接法。"
        ),
        "tags": ["工装", "隔离", "接地"],
    },
    {
        "id": "FIXTURE_FOUR_WIRE_CONTACT_RES",
        "practice_scope": "process",
        "name": "工装通路压降校核",
        "en": "fixture path voltage drop verification",
        "practice": (
            "工装载流回路的压降要在**设计阶段**校核并登记: 大电流路径用粗截面"
            "铜排/多股线并联、缩短走线、必要时开检测线(remote sense)直达 DUT; "
            "校核值存进工装的通路电阻字段并随工装版本管理。"
        ),
        "why_wrong_without_it": (
            "工装压降是**系统性偏差**: 它同向叠加在每一台产品的读数上, 不会因"
            "多测几台而抵消。行业给出的老化房实例: 20m 线缆 + 20A + 0.02Ω = 0.4V "
            "压降, 对 12V 供电件是 3% 以上 —— 全部工位都偏低, 且靠电源近的工位"
            "与远的工位**不等**。"
        ),
        "spec_example": (
            "`pw_sr5400.fixture_channel_map.trace_res_mohm` / `trace_ind_nh` / "
            "`trace_cap_pf` 字段已设计好但 0 行; PA601 -54V 轨 11.1A 下 1mΩ 即 11mV, "
            "而整定值容差只有 ±0.8V。"
        ),
        "tags": ["工装", "压降", "四线制"],
    },
    {
        "id": "MEAS_POWER_ANALYZER_NOT_DMM",
        "practice_scope": "condition",
        "name": "功率测量用功率分析仪",
        "en": "use power analyzer not DMM for power",
        "practice": (
            "效率/功率用功率分析仪(同步采样电压电流)而非万用表分测再算; 测量前"
            "做**通道相位校正**, 高频段按仪器规格加算精度; 需要时开线路滤波器。"
        ),
        "why_wrong_without_it": (
            "行业给出的对比: 万用表带宽多在 40~70Hz(台式几百 kHz), 功率分析仪"
            "1~5MHz; 对含高频谐波的 PWM 类信号, 两者有效值可差很大。而有功功率 "
            "P=UIcosφ 里的 φ 依赖电压电流相位, **通道相位误差会直接进效率读数** ——"
            "不做相位校正, 高频或低功率因数下效率值本身就不可信。"
        ),
        "spec_example": (
            "PA601 SR-1210 整机效率 86~93%(三档负载) —— 1 个百分点就是 0.86 个"
            "百分点, 与相位校正误差同量级; 规格书未要求仪器类型与校正步骤。"
        ),
        "tags": ["功率计", "效率", "相位校正"],
    },
    {
        "id": "MEAS_GROUND_IMPEDANCE_METHOD",
        "practice_scope": "condition",
        "name": "接地阻抗测量方法",
        "en": "protective earth impedance measurement",
        "practice": (
            "接地阻抗测保护接地端子到机壳/地的通路: 断开其它 PE 连接避免并联"
            "分流, 用接地阻抗测试仪按规定测试电流(通常数十 A)与时间读取值。"
        ),
        "why_wrong_without_it": (
            "不断开其它 PE 就测, 读数是**并联后的结果** —— 会显著偏小, 让不合格"
            "的保护接地通过。这是安规项里典型的「测法不对反而更危险」。"
        ),
        "spec_example": (
            "PA601 SR-2505 接地阻抗 <0.1Ω, 备注区分「单机测试」与「配合系统测试」"
            "两种场景 —— 但**未写测试电流与断开要求**。"
        ),
        "tags": ["安规", "接地", "浮地"],
    },
    {
        "id": "PRACT_THERMAL_CHAMBER_SAMPLING",
        "practice_scope": "process",
        "name": "温度试验为抽样项",
        "en": "thermal chamber testing is sampled",
        "practice": (
            "高低温工作、湿热等温度相关试验在产线上按**抽样**执行(首批/定期), "
            "不是每台都进温箱; 温箱占用时间长、节拍不匹配, 全检会导致产能崩溃。"
        ),
        "why_wrong_without_it": (
            "把型试项当全检项, 产线做不出来或节拍崩溃; 反过来把该抽检的全检, 成本"
            "失控。这是判定「全检/抽检」边界错误最常见的后果。"
        ),
        "spec_example": (
            "PA601 SR-2200 低温工作试验、SR-2201 高温工作试验、SR-2204 交变湿热 "
            "—— 均属型式/抽样试验, 与产测项(SR-1100~1618)性质不同。"
        ),
        "tags": ["抽样", "环境", "节拍"],
    },
    {
        "id": "PRACT_SAFETY_INTERLOCK_DISCHARGE",
        "practice_scope": "process",
        "name": "高压测试安全联锁与放电",
        "en": "safety interlock and discharge",
        "practice": (
            "耐压/高压项须配安全联锁与放电管理: 门开自动断开高压、试后自动放电"
            "并确认残压低于阈值; 工位带急停与栅防护。"
        ),
        "why_wrong_without_it": (
            "行业原话: 高压产品须配置安全联锁与放电管理。不放电的电容在产品上"
            "留残压, 既伤操作员也可能在下一工序炸机 —— 且这类事故往往在**量产节拍"
            "压力下**被跳过。"
        ),
        "spec_example": (
            "PA601 SR-2500/2501/2502 绝缘电压 4000Vdc/2500Vdc/500Vdc —— "
            "`pw_sr5400.fixture.interlock_type` 字段存在但 0 行。"
        ),
        "tags": ["安规", "安全", "联锁"],
    },
    {
        "id": "PRACT_MSA_GRR_ACCEPTANCE",
        "practice_scope": "process",
        "name": "测量系统分析 GRR 接受准则",
        "en": "MSA gauge R&R acceptance criteria",
        "practice": (
            "判定准则先定后测: %GRR < 10% 公差可接受; 10~30% 需客户批准; >30% "
            "需整改; 另需 ndc(可区分类别数) ≥ 5。交叉设计常用 10 件 × 3 人 × 2~3 次, "
            "随机化测量顺序。"
        ),
        "why_wrong_without_it": (
            "AIAG MSA 手册明确警告: **不能只用 GRR 当唯一接受准则** —— GRR 只评"
            "随机变差, 系统性偏倚(零点漂、量程错)它测不出来。至少要同时做偏倚与 "
            "GRR。且 MSA 是 99.73% 置信、VDA5 是约 95%, 两者数值不可直接比较。"
        ),
        "spec_example": (
            "PA601 全部判据**无 MSA 要求**。以 SR-1210 效率为例: 86/91/93% 三档, "
            "公差约 7 个百分点, 而 %GRR<10% 意味着测量系统变差要 <0.7 个百分点 —— "
            "不验 MSA 就无法说明这套判据可重复。"
        ),
        "tags": ["MSA", "GRR", "判定"],
    },
    {
        "id": "PRACT_GUARDBAND_CONFORMANCE",
        "practice_scope": "process",
        "name": "保护带与符合性判定",
        "en": "guardband and conformance decision",
        "practice": (
            "判据落在测量不确定度区间内时引入**保护带(guardband)**: 符合性区间"
            "收窄或扩展, 把误判概率(判合格实为不合格/反之)压到可接受水平; 单侧"
            "公差另用 Pgk 之类指标。"
        ),
        "why_wrong_without_it": (
            "JCGM 106 指出保护带限制了基于测量信息的错误符合性判定概率。但这条"
            "实践有个**真实代价**: 落在不确定度区的件可能被**不必要地报废或返工**。"
            "所以保护带不是越多越好, 要按制程能力定。"
        ),
        "spec_example": (
            "`pw_sr5400.test_requirement.guardband` 与 `test_case.guardband` 字段"
            "已设计但全表 0 行; `sample_size` 同样为空。"
        ),
        "tags": ["判定", "保护带", "不确定度"],
    },
    {
        "id": "PRACT_TRACE_METADATA_REQUIRED",
        "practice_scope": "process",
        "name": "测试数据元数据",
        "en": "required test metadata for traceability",
        "practice": (
            "每条测试记录须绑定: 序列号/批次、**测试脚本版本**、**仪器校准状态**、"
            "环境温湿度、量具 ID, 并可导出到 MES; 数据用于批次回溯与 SPC。"
        ),
        "why_wrong_without_it": (
            "行业结论: 数据的价值取决于采集的规范性 —— 元数据不入档, 数据之间"
            "**可比性无从谈起**。批次性异常出现时要回溯该批次的测试分布, 若无"
            "校准状态与脚本版本, 就无法区分「产品变了」与「测量系统变了」。"
        ),
        "spec_example": (
            "`pw_sr5400.instrument_ledger` 有 cal_due/probe_life_hours 字段; "
            "`test_station` 有 mes_endpoint —— 但两表均 0 行。"
        ),
        "tags": ["追溯", "元数据", "MES"],
    },
    {
        "id": "PRACT_BATCH_RELEASE_AND_REWORK",
        "practice_scope": "process",
        "name": "批次放行与返修闭环",
        "en": "batch release and rework loop",
        "practice": (
            "批次放行依据 IQC/产测/老化/型式抽样结果; 不良品进返修(FA)并回到线上"
            "复测, 返修品要与新品**分流**标识; 批次不良率与返修率进统计。"
        ),
        "why_wrong_without_it": (
            "返修品混入正常流会让不良率失真, 且返修引入的应力没有记录 —— 下次"
            "同类失效复发时查不出是不是返修造成的。行业做法明确要求返修品分流。"
        ),
        "spec_example": (
            "PA601 SR-1618 输入继电器吸合次数、SR-1430 输入继电器健康寿命告警 —— "
            "这类寿命计数数据正是返修闭环的输入, 但知识层与表结构里都无返修概念。"
        ),
        "tags": ["放行", "批次", "返修"],
    },
    {
        "id": "PRACT_SPC_TREND_ON_BURNIN",
        "practice_scope": "process",
        "name": "老化期趋势监控",
        "en": "trend monitoring during burn-in",
        "practice": (
            "老化全程记录输出电压/电流/温度并做**趋势分析**: 电流随温升平滑变化"
            "是正常; 台阶式突变或持续爬升意味某台开始异常, 配合工位电流数据定位。"
        ),
        "why_wrong_without_it": (
            "只看「老化中有没有报警」会漏掉渐变型异常 —— 而渐变正是批次性问题的"
            "早期特征。等到报警时已是一台坏件, 而不是一批趋势偏移。"
        ),
        "spec_example": (
            "PA601 SR-1609 风扇转速、SR-1606 环境温度、SR-1608/1613~1616 热点温度"
            "都是可趋势化的遥测量, 但无任何趋势判据(如斜率阈值)。"
        ),
        "tags": ["老化", "趋势", "SPC"],
    },
    {
        "id": "PRACT_POWER_MARGIN_AND_TIERED_PROTECTION",
        "practice_scope": "process",
        "name": "仪器功率余量与分级保护",
        "en": "instrument power margin and tiered protection",
        "practice": (
            "选仪留功率余量(短时老化 70~80% 连续额定, 7×24 量产留 1.5 倍以上);"
            "并联母线上电源限流按台数留余量 + 工位级熔断隔离, 形成**两级保护**:"
            "电源侧保母线, 工位侧隔离单台故障。"
        ),
        "why_wrong_without_it": (
            "不余量会在长时间满载下触发过热降额/过温保护, 造成老化中断、批次数据"
            "作废。只靠电源单级限流时, 一台短路会独占电流把其余工位拉低 —— "
            "一台坏件毁掉整批老化的有效性。"
        ),
        "spec_example": (
            "PA601 -54V 轨 600W, SR-1204 备注「90~176Vac: 400W; 176~286Vac: 600W」"
            "—— 低压输入段功率降额, 选仪须按 600W 而非 400W。"
        ),
        "tags": ["选型", "保护", "余量"],
    },
    {
        "id": "MEAS_LOAD_SHARE_SYNC_SAMPLING",
        "practice_scope": "condition",
        "name": "并联均流同步采样测量",
        "en": "synchronized sampling for current sharing measurement",
        "practice": (
            "均流不平衡度在额定电流 10/25/50/75/100% 五点各测: 每点稳定 ≥5min "
            "后**同步**读取所有并联模块输出电流; 判据 (max|Ii−Ī|/Ī)×100%, 半载"
            "及以上常规限 ±5%、轻载放宽(参考 ±10%, 型号限值以规格书为准)。测前"
            "逐台单机校准输出电压整定与限流特性; 各模块输出线长/截面保持一致。"
            "动态均流做 50%→100% 负载阶跃, 记录超调与均流恢复时间。"
        ),
        "why_wrong_without_it": (
            "不同步读取等于把模块电流的**时间差**当失衡量: 负载波动期间先后读数, "
            "偏差里混着工况漂移, 会把合格品测成超差或反之。输出线不等长引入的线路"
            "压降差同一量级(长线细线抬高等效下垂斜率), 批评「模块不均流」而实为"
            "接线不对称是均流测量最常见的误诊(Glary AN 明确要求线束对称; 中析检测"
            "CMA/CNAS 方法同样把「输出电缆长度截面积一致」列为前置)。"
        ),
        "spec_example": (
            "PA601 SR-1219 负载均流度 — 备注却写「不要求均流度, 但不能出现并联电源"
            "中的单体不停进行空满载切换带载现象; 没有预留均流母线, 推荐下垂均流法」"
            "—— 判据口径本身需澄清后再行; 但「不出现空满载切换」的验证仍需要**并机"
            "动态测试**能力, 这部分不因「不要求均流度」而消失。"
        ),
        "tags": ["均流", "并联", "多通道同步"],
    },
    {
        "id": "MEAS_TEMPCO_CHAMBER_SWEEP",
        "practice_scope": "condition",
        "name": "温度系数扫温测量",
        "en": "temperature coefficient sweep in chamber",
        "practice": (
            "温度系数在**能控温的环境腔**里测: 取规格书工作温度范围两端 + 常温 ≥3 "
            "个温度点, 每点热浸稳定(DUT 按工况带载)后同步记录输出电压与 DUT 处温度, "
            "(ΔV/V)/ΔT 最小二乘拟合。读数用 6 位半量级 DMM, 温度用热电偶/巡检仪"
            "(T 型覆盖 −200~+400℃)。判据参考量级 ±0.02%/℃ 属精密测量。"
        ),
        "why_wrong_without_it": (
            "**没有温箱就物理上测不了** —— 这条判据的仪器承载者是腔体而不是表: "
            "室温漂移 1℃ 就能吃掉 ±0.02%/℃ 判据的整个允差带, 靠空调房间与「读遥测"
            "温度」是把环境当激励的赌博。温箱还要**带载**标定(DUT 额定功耗的热负荷"
            "落在腔体能力内, 否则腔温被 DUT 自身发热顶住, 温度点是名义的而不是实际"
            "的)—— GB/T 2424.7-2024 专门为带负载的温度箱测量给出导则。"
        ),
        "spec_example": (
            "PA601 SR-1217 温度系数 3.45V 轨与 -54V 轨(±0.02%/℃) —— 现有落库 "
            "instrument_need 经 CAPABILITY_BY_KIND『thermal』传播 thermal_chamber, "
            "但要求书里从未写明温箱能力口径(温度范围/稳定度/带载), 选型无依据。"
        ),
        "tags": ["温度系数", "温箱", "精密测量"],
    },
)


def build_production_practice() -> list[dict[str, Any]]:
    """:data:`PRODUCTION_PRACTICE` -> ``power_concept`` 实体。

    **不入型号库**: 产测工艺跨型号共用(任何通信整流器都按 20MHz 限带测纹波),
    放型号库会读成「这套测法只在 PA601 上有记录」。

    ``authority_kind`` 一律 ``unverified`` 且**不给** ``authority_ref``: 这些做法
    业界通行但本项目拿不到可引的标准条款号(纹波测法在 TI 应用笔记、老化时长在
    各厂商实践里), 标 ``standard`` 会让「出处必须可查」变成空话。信度 0.5 —
    比 ``unverified`` 的 0.2 高一档, 因为这些做法有**多方业界共识**支撑,
    不是凭空来的; 但仍低于有条款号的标准级知识。

    ``why_wrong_without_it`` 是本模块的核心: 每条都要能回答「不这么做会测错成
    什么样」。只写做法不写后果, 读者无从判断能不能省这一步。

    **``practice_scope`` 区分两层, 不能省**: 这批知识不是同一层次的东西。纹波 20MHz 限带
    决定的是「这条判据怎么测」, 应当被 ``config/test_methods.yaml`` 的方法引用
    并落进 ``condition_vector``; 而老化梯度/AQL 抽样/MSA GRR 管的是「产线怎么
    管控」, 落进某条判据的测试条件里属于错位 —— 一条判据的测试条件里出现「按
    1.5 倍余量选仪器」是噪声, 不是条件。

    有了 ``practice_scope``, 知识层才能被反向断言: ``scope=condition`` 的必须被至少一个
    方法引用, 否则就是「入库了但没人用」; 而 ``practice_scope=process`` 的没被引用是本分。
    没有它只能报一个总计数, 而计数驱动不了任何补齐工作。
    """
    out: list[dict[str, Any]] = []
    for spec in PRODUCTION_PRACTICE:
        text = f"{spec['id']}: {spec['name']} {spec['en']} — 做法: {spec['practice']}"
        out.append(
            {
                "id": spec["id"],
                "name": spec["name"],
                "type": "power_concept",
                "text": text,
                "source": "产测工艺(websearch 查证, 2026-10-07)",
                "properties": _props(
                    {
                        "zh": spec["name"],
                        "en": spec["en"],
                        "definition": spec["practice"],
                        # 两段并列存: 前者是做法, 后者是「省掉的后果」。
                        "note": (
                            f"{spec['why_wrong_without_it']}\n【规格书实例】{spec['spec_example']}"
                        ),
                        "practice": spec["practice"],
                        "consequence_if_skipped": spec["why_wrong_without_it"],
                        "spec_example": spec["spec_example"],
                        "synonyms": [spec["name"], spec["en"]],
                        "tags": list(spec["tags"]),
                        # condition = 这条知识决定「判据怎么测」, 应被方法引用;
                        # process = 管「产线怎么管控」, 不进 condition_vector。
                        # 见本函数 docstring: 没有它反向门禁只能报计数。
                        "practice_scope": spec["practice_scope"],
                        "authority_kind": "unverified",
                        "authority_ref": None,
                        # 0.5 = 有多方业界共识但无可引条款; 见函数 docstring。
                        "credibility": 0.5,
                        "provenance_kind": "websearch_verified_practice",
                    },
                    "industry_practice",
                    0,
                    authority_kind="unverified",
                    confidence=0.5,
                ),
            }
        )
    return out


#: **被引用但方案 md 未列的标准**(2026-10-07 websearch 查证)—— 图稀疏层 1。
#:
#: 起因(图诊断实测): 有 12 个标准号被 61 处 ``authority_ref`` 引用, 但种子里
#: **没有对应的 ``std::`` 实体**, 于是 :func:`build_relationships` 的
#: ``f"std::{head}" in ids`` 判断全部落空, **一条 ``defined_by`` 边都建不出来**,
#: 且不报错。这些标准号只出现在各实体的 ``authority_ref`` 文本里, 方案 md 的
#: 标准表里没有 —— 所以 ``extract_standards`` 抽不到它们。
#:
#: **为什么补实体而不是改引用**: 缺的是**实体这一端**。那 61 处引用是既有的引用,
#: 补一端就够; 反过来去改/删引用是把已有知识改掉。
#:
#: **只注入查证过的**(红线 5: 出处必须可查)。这三条是上一轮门禁报出来后补查的:
#: ``GB/T 14598.127-2013``(≡IEC 60255-127:2010, 含复归特性, 与引用它的三条
#: ``SET_*_RETURN`` 类别吻合)、``GB/T 2900.32-1994``、``GB/T 2900.93-2015``。
#: 后两条的**条款归属存疑**, 只注入实体(标准确实存在且现行), 存疑写进 ``note``,
#: 并由门禁 ``authority_clause_missing`` 继续报 —— **不因为标准真实就把条款归属也
#: 认了**, 那是两件事(与 ``build_referenced_standards`` 的 docstring 同一个道理)。
#:
#: 剩下这 4 类**故意不注入**, 它们不是「缺实体」:
#:
#: - ``std::IEC 60721-3-1/3-2`` / ``std::IEC 60898-1/2``: 实体 id 本身是**两条标准的
#:   合写**(斜杠分隔), 而 ``authority_ref`` 只能填一个标准号 —— 结构性不可解析。
#:   补一个 ``std::IEC 60898-1`` 实体等于把一条标准拆成两个 id, 那会造出「一个标准
#:   号对应两个实体」的分裂, 比现在不可解析更糟。
#: - ``INSTR_EMC_RECEIVER`` 引 ``GB/T 17626 系列``: 引用刻意写成**系列**(整族 EMC
#:   试验标准), 没有指向具体一条。补 14 条 GB/T 17626.x 实体是凭空造数据。
#: - ``COND_VIN_NOM`` 引 ``GB/T 2900.1 电工术语; IEC 60050-151``: 一条引用里
#:   给了**两条标准**(且前者没写部分号), 提取器只能取到第一条。
#: - ~~``CONFORMAL_COATING`` 引 ``IEC 60664-1 / IPC-2221B``~~: **已不在此列**。
#:   该实体与其标准 ``std::IPC-2221`` 已于 2026-10-08 按「绑不上产测流程」移出范围,
#:   见 :data:`OUT_OF_SCOPE_ENTITY_IDS`。移出前它的引用确实无法解析(复合引用 +
#:   IPC-2221B 已被 C 版代替 + IEC 60664-1 没写年份), 但那是次要理由 ——
#:   主要理由是**它属于印制板设计/组装侧, 不是电源产品产测侧**。
#:   同源的 ``POLLUTION_DEGREE`` 保留: 它是判读耐压/漏电流实测结果的输入。
#:
#: ``status`` 只填**查到**的那一个, 查不到就留 ``UNVERIFIED``, 不猜。
#:
#: **``GB/Z 14429-2005`` 的特别说明**(它一个人占 32 条引用): 该标准是《远动设备及
#: 系统 第1-3部分:总则 术语》(≡IEC 60870-1-3:1997), 即**电力调度自动化/远动
#: 终端**的术语表。引用它的 32 个实体是 ``YC_*``遥测 / ``YX_*``遥信 /
#: ``YK_*``遥控 —— 远动四遥量, **类别对得上**。但那 32 条里**只有 1 条带条款号**
#: (``INSTR_SOE_TESTER`` -> §2.1.45 事故追忆), 其余 31 条只写标准号不写条款。
#: 补上实体后这 31 条会各自获得一条 ``defined_by`` 边、看起来更权威, 而实质上
#: 「标准号可解析」不等于「该量的定义在那一页」。所以门禁另设
#: ``authority_clause_missing``: standard 级引用**不带条款号**判 WARN。
#:
#: **``GB 3187-1994`` 已废止**(2009-04-01), 由 ``GB/T 2900.13-2008`` 代替,
#: 后者又被 ``GB/T 2900.99-2016`` 部分代替。仍然注入: 引用废止标准是可接受的
#: (只要标了 ``SUPERSEDED`` 与代替关系), 保留它才能回答「历史报告依据的是哪一版」。
REFERENCED_STANDARDS: tuple[dict[str, Any], ...] = (
    {
        "id": "std::GB/Z 14429-2005",
        "title": "远动设备及系统 第1-3部分:总则 术语",
        "title_en": "Telecontrol equipment and systems. Part 1: General considerations. Section 3: Glossary",
        "status": "CURRENT",
        "published": "2005-02-06",
        "implemented": "2005-12-01",
        "replaces": "GB/T 14429-1993",
        "iec_equivalent": "IEC 60870-1-3:1997",
        "note": "复审 2024-08-23 结论「继续有效」。范围是远动设备(电网调度自动化)的"
        "术语, 不是电源通用术语表 —— 引用它的 YC_/YX_/YK_ 是远动四遥量。",
    },
    {
        "id": "std::GB/T 2900.70-2008",
        "title": "电工术语 电器附件",
        "title_en": "Electrotechnical terminology - Electrical accessories",
        "status": "CURRENT",
        "published": "2008-01-22",
        "implemented": "2008-09-01",
        "iec_equivalent": "IEC 60050-442:1998",
        "note": "IDT。额定值(442-01-01)与标称值(442-01-04)的出处 —— "
        "RATED_VALUE / NOMINAL_VALUE 引的是它。",
    },
    {
        "id": "std::GB/T 2900.56-2008",
        "title": "电工术语 控制技术",
        "title_en": "Electrotechnical terminology - Control technology",
        "status": "CURRENT",
        "published": "2008-06-18",
        "implemented": "2009-05-01",
        "iec_equivalent": "IEC 60050-351:2006",
        "note": "IDT。相位裕度(§351-25-05)、增益裕度(§351-25-07)、增益交越角频率"
        "(§351-25-04)、控制建立时间(§351-25-02)、超调量(§351-24-30)的出处。",
    },
    {
        "id": "std::GB/T 2900.33-2004",
        "title": "电工术语 电力电子技术",
        "title_en": "Electrotechnical terminology - Power electronics",
        "status": "CURRENT",
        "published": "2004-05-10",
        "implemented": "2004-12-01",
        "iec_equivalent": "IEC 60050-551:1998; IEC 60050-551-20:2001",
        "note": "IDT 两条。谐波族(§551-20-03~14)的出处。",
    },
    {
        "id": "std::GB/T 2900.89-2012",
        "title": "电工术语 电工电子测量和仪器仪表 第2部分:电测量的通用术语",
        "title_en": "Electrotechnical terminology - Electrical and electronic measurements and instruments - Part 2: General terms relating to electrical measurements",
        "status": "CURRENT",
        "iec_equivalent": "IEC 60050-312:2001",
        "note": "纹波(§312-07-02)、噪声(§312-07-04)的出处。",
    },
    {
        "id": "std::GB/T 2900.74-2008",
        "title": "电工术语 电路理论",
        "title_en": "Electrotechnical terminology - Circuit theory",
        "status": "CURRENT",
        "iec_equivalent": "IEC 60050-131:2002",
        "note": "MOD(修改采用)。sym::R_in(线路电阻)的出处。",
    },
    {
        "id": "std::GB/T 3187-1994",
        "title": "可靠性、维修性术语",
        "title_en": "Reliability and maintainability terms",
        "status": "SUPERSEDED",
        "published": "1994-12-06",
        "implemented": "1995-07-01",
        "abolished": "2009-04-01",
        "replaced_by": "GB/T 2900.13-2008",
        "iec_equivalent": "IEC 60050-191:1990",
        "note": "**已于 2009-04-01 废止**, 由 GB/T 2900.13-2008《电工术语 可信性与"
        "服务质量》(≡IEC 60050-191:1990)全部代替; 后者又被 GB/T 2900.99-2016"
        "部分代替。保留是因为 MTBF 的 §7.2.8 出处在这一版, 且要能回答"
        "「历史报告依据的是哪一版」。",
    },
    {
        "id": "std::GB/T 14598.127-2013",
        "title": "量度继电器和保护装置 第127部分:过/欠电压保护功能要求",
        "title_en": "Measuring relays and protection equipment - Part 127: "
        "Functional requirements for over/under voltage protection",
        "status": "CURRENT",
        "published": "2013-07-19",
        "implemented": "2013-12-02",
        "iec_equivalent": "IEC 60255-127:2010",
        "note": "IDT。**类别与引用者对得上**: 该部分含复归特性/复归时间/复归系数"
        "(§4.2 复归特性、附录A 仅有跳闸输出的继电器的复归时间测定), 而引用它的"
        "三条正是 SET_OCP_RETURN / SET_OVP_RETURN / SET_OTP_RETURN(保护复归)。",
    },
    {
        "id": "std::GB/T 2900.32-1994",
        "title": "电工术语 电力半导体器件",
        "title_en": "Electrotechnical terminology - Power semiconductor devices",
        "status": "CURRENT",
        "published": "1994-05-16",
        "implemented": "1995-01-01",
        "replaces": "GB 2900.32-1982",
        "note": "由强制性 GB 转为推荐性 GB/T。参照采用 IEC 出版物 747 与 IEC 50(521)。"
        "**⚠ 引用存疑**: DERATING 引的是它的 §2.2.9 热降额因数, 但「热降额」在电源"
        "语境下更可能出自半导体变流器(GB/T 3859)或电路理论(GB/T 2900.74)那几部分。"
        "本轮**未核到 §2.2.9 的原文**, 所以只注入标准实体(它确实存在且现行), "
        "不确认条款归属 —— 门禁的 authority_clause_missing 会继续报。",
    },
    {
        "id": "std::GB/T 2900.93-2015",
        "title": "电工术语 电物理学",
        "title_en": "Electrotechnical terminology - Physics for electrotechnology",
        "status": "CURRENT",
        "published": "2015-09-11",
        "implemented": "2016-04-01",
        "iec_equivalent": "IEC 60050-113:2011",
        "note": "IDT。范围是空间和时间、一般宏观概念、力、热力学、粒子物理学、"
        "固体物理学。**⚠ 引用存疑**: sym::τ_noise(噪声时间常数)引的是它, 但"
        "「噪声时间常数」不像是电物理学条目, 更可能在 GB/T 2900.74(电路理论)"
        "或专门的噪声标准里。同上: 只注入实体, 不确认条款归属。",
    },
)


def build_referenced_standards() -> list[dict[str, Any]]:
    """:data:`REFERENCED_STANDARDS` -> ``standard`` 实体。

    ``authority_kind`` 给 ``standard``, 依据是**标准号本身已核实存在且现行** ——
    这是比「某概念的某条定义出自它」弱得多的主张, 不需要逐条条款支撑。引用它的
    概念**是否引对了条款**, 由门禁的 ``authority_clause_missing`` 单独判 ——
    两件事不能混成一件: 标准真实存在 ≠ 每个引用都核到了条款。
    """
    out: list[dict[str, Any]] = []
    for spec in REFERENCED_STANDARDS:
        num = spec["id"].replace("std::", "")
        conf = 0.95 if spec.get("status") in {"CURRENT", "SUPERSEDED"} else 0.7
        out.append(
            {
                "id": spec["id"],
                "name": spec["title"],
                "type": "standard",
                "text": f"{num} {spec['title']}",
                "source": num,
                "properties": _props(
                    {
                        "title": spec["title"],
                        "title_en": spec.get("title_en"),
                        "status": spec.get("status", "UNVERIFIED"),
                        "published": spec.get("published"),
                        "implemented": spec.get("implemented"),
                        "abolished": spec.get("abolished"),
                        "replaces": spec.get("replaces"),
                        "replaced_by": spec.get("replaced_by"),
                        "iec_equivalent": spec.get("iec_equivalent"),
                        "note": spec.get("note"),
                    },
                    "standard",
                    None,
                    authority_kind="standard",
                    authority=num,
                    confidence=conf,
                ),
            }
        )
    return out


#: **零引用且未查证的标准实体** —— 按**规则**算, 不维护 id 清单(2026-10-07)。
#:
#: 实测 133 个标准实体里 **109 个完全无人引用**: 无关系边, 且其它记录的
#: ``source`` / ``clause`` / ``authority_ref`` / ``text`` / ``title`` / ``note`` /
#: ``scope`` 里都不出现其编号。其中 106 个是 ``unverified``(从没查证过)。
#:
#: 它们是从方案 md 的标准表抄进来、此后**从未被任何东西用到**的清单条目。按已用过的
#: 处置逻辑(6 条同名重复概念、:data:`LIFETIME` 无标准术语依据 + 零消费者 -> 移除),
#: 「零引用 + 未查证」就是移除条件。
#:
#: ## 为什么按规则算而不是列清单
#:
#: 109 个 id 的硬编码清单会**烂掉**: 将来有人引用了其中一条, 清单不会知道, 于是
#: 一条**有引用**的标准被静默删掉, 而它的引用者变成悬空。按规则算则每次生成都
#: 重新判定, 「被引用了」立刻生效。
#:
#: ## 「零引用」怎么判 —— 只看实体, 不看关系
#:
#: :func:`build_relationships` 建指向标准的边**只有一条路径**: 某实体的
#: ``authority_kind == "standard"`` 且 ``authority_ref`` 抽出的标准号命中。所以
#: 「有没有边指向它」等价于「有没有别的实体的 ``authority_ref`` 命中它」—— 用
#: 实体就能判, 不必等关系建完(关系是在实体过滤**之后**才建的)。
#:
#: ## 额外一道: 编号在**文本**里出现过的**保留**
#:
#: 只判 ``authority_ref`` 会漏掉一种情况: 某条记录在 ``source`` 或 ``note`` 里
#: 提了这个标准号(人读得懂, 机器解析不了)。那种情况下标准实体虽无边, 但它是
#: **可被人查到的线索**, 保留它。
#:
#: **这条豁免不能当保命符用**: 它曾让 ``std::IPC-2221`` 活下来 —— 而它与
#: ``CONFORMAL_COATING`` 已于 2026-10-08 按「绑不上产测流程」移出范围
#: (见 :data:`OUT_OF_SCOPE_ENTITY_IDS`)。**「有人提到它」不等于「该有它」**:
#: 文本提及只能说明这条知识还引用着它, 而引用它的那条知识本身可能就不在范围内。
#: 所以范围排除必须发生在这一层**之前**, 不能指望靠文字提及把出范围的东西留下。
#:
#: **非 ``unverified`` 的一律保留** —— ``industry`` / ``standard`` 级说明有人工
#: 判断过, 那个判断不因为「当前零引用」而失效。
_TEXT_CITATION_FIELDS = (
    "source",
    "clause",
    "authority_ref",
    "text",
    "title",
    "note",
    "scope",
)


def find_dead_standards(entities: list[dict[str, Any]]) -> list[str]:
    """:data:`DEAD_STANDARD` 规则的实现 -> 移除候选 id 列表。

    判据三条全中才算「死」: ``unverified`` + 无 ``authority_ref`` 命中 + 编号在
    别处文本里不出现。

    **属性在这个阶段还是嵌套的** —— ``prune_non_executable`` 里的 ``props_of``
    正是 ``e.get("properties") or {}``, 扁平化发生在更后面。所以这里必须走
    ``properties``, 直接读 ``e["authority_kind"]`` 恒为 ``None``, 规则会**静默
    返回空** —— 实测踩过一次: 条件永不成立, 移除数 0, 而生成日志里连一行都没有。
    """

    def p(e: dict[str, Any]) -> dict[str, Any]:
        return e.get("properties") or {}

    stds = {str(e["id"]): e for e in entities if e.get("type") == "standard"}
    if not stds:
        return []

    cited_by_ref: set[str] = set()
    cited_in_text: set[str] = set()
    for e in entities:
        eid = str(e.get("id") or "")
        if eid in stds:
            continue  # 标准自己的 title/source 里当然有它自己的编号
        props = p(e)
        aref = props.get("authority_ref")
        if isinstance(aref, str):
            m = _STD_ID_RE.match(aref)
            if m:
                head = re.sub(r"\s+", " ", m.group(0)).strip()
                cited_by_ref.add(f"std::{head}")
        for f in _TEXT_CITATION_FIELDS:
            v = str(props.get(f) or "")
            if not v:
                continue
            for sid, se in stds.items():
                if str(sid.removeprefix("std::")) in v or str(se.get("name") or "") in v:
                    cited_in_text.add(sid)

    dead: list[str] = []
    for sid, e in sorted(stds.items()):
        if p(e).get("authority_kind") != "unverified":
            continue
        if sid in cited_by_ref or sid in cited_in_text:
            continue
        dead.append(sid)
    return dead


def _out_of_scope_extra(entities: list[dict[str, Any]]) -> frozenset[str]:
    """零引用未查证标准 -> 移出范围。**在生成时判定并计数**, 不静默丢。"""
    dead = find_dead_standards(entities)
    if dead:
        print(
            f"  [移出范围] 零引用且未查证的标准 {len(dead)} 条"
            f"(零关系边 + 别处文本无引用 + authority_kind=unverified), "
            f"示例 {dead[:5]}"
        )
    return frozenset(dead)


def _out_of_scope_static() -> frozenset[str]:
    return frozenset(OUT_OF_SCOPE_ENTITY_IDS | OUT_OF_SCOPE_FORMULA_IDS | DUPLICATE_CONCEPT_IDS)


def strip_conflated_synonyms(entities: list[dict[str, Any]]) -> list[str[str]]:
    """摘掉 :data:`CONFLATED_SYNONYMS` 里那些把额定/标称混同的别名。

    **为什么必须在生成阶段摘**: 留着它们, 检索会把「标称输出电压」当成额定输出
    电压的另一种写法返回, 而 GB/T 2900.70 把两者定义为两个不同条目 ——
    额定是规定工作条件下的保证值, 标称是标识用近似值。混同的代价不是「多召回
    了一条」, 而是**产测判据可能取到不承诺性能的值**。

    摘而不是加 ``note``: 同义词是检索用的等价集, 在里面放一对不等价的词, 等于
    让检索把它们当同一个 —— 那正是要修的问题本身。
    """
    by_id = {e["id"]: e for e in entities}
    removed: list[str] = []
    for cid, alias, basis in CONFLATED_SYNONYMS:
        props = by_id.get(cid, {}).get("properties") or {}
        syn = list(props.get("synonyms") or [])
        if alias not in syn:
            # 清单项已经不在同义词里 —— 可能上游 corrections.yaml 已处理(实测
            # 「标称输出电压」就是如此)。**仍要记账**: 陈旧清单项静默无输出时,
            # 没人知道它是被谁摘的, 而下一次上游不再摘它时就会悄悄失效。
            dropped = props.setdefault("dropped_synonyms", [])
            if not any(d["alias"] == alias for d in dropped):
                dropped.append({"alias": alias, "basis": basis, "state": "already_absent"})
            removed.append(f"{cid}: '{alias}' 已不存在(上游已处理, 清单项已陈旧)")
            continue
        syn.remove(alias)
        props["synonyms"] = syn
        removed.append(f"{cid}: -'{alias}' ({basis})")
        # 记下被摘的别名与依据, 审计时能回答「为什么这个别名没了」
        dropped = props.setdefault("dropped_synonyms", [])
        if not any(d["alias"] == alias for d in dropped):
            dropped.append({"alias": alias, "basis": basis, "state": "removed"})
    return removed


def extract_concepts(lines: list[str]) -> list[dict[str, Any]]:
    """概念字典(§6.3.1) -> ``power_concept``。

    概念字典在方案里**出现两次**(第 3160 与第 15140 行起)。按 ID 去重并留
    **第一次**出现的位置 —— 留后一份会让 ``source_ref`` 指向复制处。
    """
    out: dict[str, dict[str, Any]] = {}
    in_table = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if _CONCEPT_HEADER.match(stripped):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped.startswith("|"):
            if stripped.startswith("#"):
                in_table = False
            continue
        cells = _cells(stripped)
        if len(cells) < 3 or set(cells[0]) <= {"-", ":"}:
            continue
        cid = _clean(cells[0])
        if not cid or cid in out:
            continue
        zh, en = _clean(cells[1]), _clean(cells[2])
        alias = _clean(cells[3]) if len(cells) > 3 else None
        # 别名列拆成数组并入 ``synonyms`` —— 早先写成标量 ``aliases``, 而 schema
        # 声明的检索字段是 ``synonyms``(数组), 且**没有任何消费方读 aliases**,
        # 于是 118 条概念的另一种叫法对检索完全不可见, 而检索照样返回结果。
        # 少召回是隐形的, 看不出来。synonyms 同时收 zh/en, 使同义集自洽。
        synonyms: list[str] = []
        for _s in [zh, en, *(x.strip() for x in _ALIAS_SPLIT.split(alias or ""))]:
            if _s and _s not in synonyms:
                synonyms.append(_s)
        qudt = None
        if len(cells) > 4 and cells[4].startswith("qudt:"):
            qudt = cells[4].split(":", 1)[1].strip()
        out[cid] = {
            "id": cid,
            "name": zh or en or cid,
            "type": "power_concept",
            "text": f"{cid}: {zh or ''} {en or ''}".strip(": "),
            "properties": _props(
                {"zh": zh, "en": en, "synonyms": synonyms or None, "qudt_ref": qudt},
                "bootstrap_section",
                i,
            ),
        }
    return list(out.values())


_U5_HEADER = re.compile(r"^\|\s*符号\s*\|\s*含义\s*\|")


def _split_symbols(cell: str) -> list[str]:
    """按逗号切符号格, **但不切反引号内的逗号**。

    U.5 里两种写法并存:

    - `` `V_in`, `V_out`, `V_ref` `` —— 每个符号各自加反引号, 逗号是分隔符
    - `` `T_j,max` `` —— **一个**符号, 逗号在下标里表示「T_j 的最大值」

    按逗号盲切会把 ``T_j,max`` 拆成 ``T_j`` 与 ``max`` 两个符号, 而 ``max``
    会被当成一个真有量纲 ``[K]`` 的量 —— 那个量纲其实是「最高结温」的, 挂在
    ``max`` 上看起来完全正常。这类错误不报错, 只会让下游推理多出一个叫
    「max」的热学量。
    """
    parts: list[str] = []
    buf: list[str] = []
    in_code = False
    for ch in cell:
        if ch == "`":
            in_code = not in_code
            continue
        if ch in ",，" and not in_code:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def extract_symbols(lines: list[str]) -> list[dict[str, Any]]:
    """U.5 符号表 -> ``symbol``。

    ``| `V_in`, `V_out` | 电压 | `[V]` | `E` |`` —— 一格可含**多个符号**,
    逐个拆成独立节点(否则 ``V_in`` 与 ``V_out`` 会挤成一个节点, 关系连不上)。

    量纲未定的(方案写「见各条」或格子数不等)也收, 但 ``dimension=None``:
    收进来是为了让关系边完整, 而不是因为它可参与计算。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    in_table = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if _U5_HEADER.match(stripped):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped.startswith("|"):
            if stripped.startswith("#"):
                in_table = False
            continue
        cells = _cells(stripped)
        if len(cells) < 3 or set(cells[0]) <= {"-", ":"}:
            continue
        meaning, dim_text = _clean(cells[1]), _clean(cells[2])
        ns = _clean(cells[3]) if len(cells) > 3 else None
        names = [n for n in (_clean(s) for s in _split_symbols(cells[0])) if n]
        # **量纲按位置对应**, 不能把整格的量纲发给每个符号。
        # U.5 里 ``R_ESR`` 与 ``R_ESL`` 写在同一格, 量纲格是 ``[Ω],[H]`` ——
        # 一一对应。早先一版给两个符号都发 ``[Ω],[H]``, 于是 ``R_ESR``(电阻)
        # 带着电感的量纲, 而这种错会一路传到齐次性判定而不报错。
        dims = [d for d in (_clean(x) for x in re.split(r"[,，/]", dim_text or "")) if d]
        for index, name in enumerate(names):
            if name in seen:
                continue
            seen.add(name)
            # 单符号多量纲(如 ``[K] 或 [℃]``)保留全部; 多符号时按位置取,
            # 位置越界说明表格本身对不齐, 标 None 而不是猜。
            if len(dims) == len(names):
                own_dim = dims[index]
            elif len(dims) == 1 or len(names) == 1:
                own_dim = dim_text
            else:
                own_dim = None
            out.append(
                {
                    "id": f"sym::{name}",
                    "name": name,
                    "type": "symbol",
                    "text": f"{name}: {meaning}" if meaning else name,
                    "properties": _props(
                        {
                            "zh": meaning,
                            "dimension": own_dim,
                            "namespaces": [ns] if ns else None,
                            "dimension_declared": bool(
                                own_dim and own_dim not in ("见各条", "无量纲")
                            ),
                        },
                        "bootstrap_section",
                        i,
                    ),
                }
            )
    return out


#: 公式表行: | `F_J.2.1_BUCK` | `V_out = D × V_in` | `[V]` | A-2, A-4, T3 |
#:
#: ID 必须**独占一格**(整格就是 ID), 不能用「行里出现 F_ 就算」—— 后者会把
#: 「某规则的公式列写着 F_J.2」这类引用行也当成公式, 实测那样只抽到 11 条。
_FORMULA_ROW = re.compile(r"^\|\s*`(?P<fid>F_[A-Z]\.[\d.]+(?:_[A-Z0-9_]+)?)`\s*\|")

#: 表格里表示「本格没有内容」的占位符
_PLACEHOLDER = frozenset({"\u2014", "\u2013", "-", "/", "N/A", "n/a", "TBD", "\u5f85\u5b9a"})

#: 规则 / 测试 / 公理的编号 (``P3`` / ``G.28`` / ``T24`` / ``A-1.5``)。
#: 单独成格时它是**引用**而非内容 —— 必须在字母后紧跟数字, 否则中文说明
#: 里的「A 类」这类字样会被误判。
_REF_ONLY = re.compile(r"[AFGRPETUW]-?[\d.]+\w*")


def extract_formulas(lines: list[str]) -> list[dict[str, Any]]:
    """公式表 -> ``formula``。

    ``| `F_J.2.1_BUCK` | `V_out = D × V_in` | `[V]` | A-2, A-4, T3 |``

    **保留原始表达式文本**, 不做归一化 —— 归一化是为量纲齐次性服务的, 而那不是
    本层的职责; 概念层要的是「这条式子说了什么」, 下游读到的也应是工程师写的
    式子, 而不是被改写过的形式。

    ``declared_dimension`` 存方案自己写的量纲列(``[V]`` 之类)。**照抄, 不校验**
    —— 校验是另一层的职责; 这里只负责「方案说了什么」, 标明它只是方案的声明。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        m = _FORMULA_ROW.match(stripped)
        if m is None:
            continue
        fid = m.group("fid")
        cells = _cells(stripped)
        if len(cells) < 2:
            continue
        first = _clean(cells[1])
        # 下面两类行**首格是公式 ID, 但整行不是公式定义**。收进来会得到一条
        # text 等于自身 id 的空壳: 既检索不到东西, 又虚增公式总数。
        #
        # 1. 交叉引用行 (公式 ↔ 规则 ↔ 测试 ↔ 公理 映射表):
        #    | `F_K.5.2_LYAPUNOV_LTI` | P3 | G.28 | T24 |
        #    第二节是**编号**, 不是内容 —— 这行在说「谁引用了这条公式」。
        # 2. 分区目录行 (附录 U.2.7 按小节汇总):
        #    | `F_W.5` | 模拟前端（虚短虚断、…） | 18 | T21, T24 |
        #    第三节是**条目计数**(裸整数), 既不是量纲也不是上游 —— 首格那个
        #    是小节名, 不是公式。
        #
        # 早先只靠「有没有解析出表达式」兜底, 结果这 53 条全部退化成 text=id:
        # 表达式列缺失时无处可退, 只能把 id 本身当内容写进去。
        if first is None or first in _PLACEHOLDER or _REF_ONLY.fullmatch(first):
            continue
        if len(cells) >= 3 and (_clean(cells[2]) or "").isdigit():
            continue
        # 第一个含等号或运算符的格是表达式; 再往后是量纲与公理列
        expr = dim = upstream = desc = None
        for c in cells[1:]:
            text = _clean(c)
            if text is None:
                continue
            if expr is None and re.search(r"[=\u2248\u2264\u2265<>\u00b7\u00d7/]", text):
                expr = text
                continue
            if dim is None and (text.startswith("[") or "无量纲" in text):
                dim = text
                continue
            if upstream is None and re.fullmatch(
                r"[AFGRPETUW][\w.]*(\s*[,\uff0c]\s*[AFGRPETUW][\w.]*)*", text
            ):
                upstream = [t for t in re.split(r"[,\uff0c]", text)]
                continue
            # 既不是表达式也不是量纲/上游, 但仍是实打实的内容(中文说明、
            # 引文式判据)。留着它 —— 好过让 text 退化成 id。
            if desc is None:
                desc = text
        if expr is None and desc is None:
            continue
        if fid in seen:
            continue
        seen.add(fid)
        # **章节号取自公式 ID 本身**(``F_J.2.1_BUCK`` -> ``J.2.1``), 不用「最近的
        # markdown 标题」: 公式总表物理上位于附录 §18.10, 按标题定位会让 570 条
        # 公式的出处全指向 §18.10, 追不回「这条式子原本在方案哪一节」。
        own = re.match(r"^F_([A-Z]\.[\d.]+)", fid)
        sec = own.group(1) if own else None
        out.append(
            {
                "id": fid,
                "name": fid,
                "type": "formula",
                "text": f"{fid}: {expr or desc}",
                "properties": _props(
                    {
                        "section": sec,
                        "expr": expr,
                        "declared_dimension": dim,
                        "upstream": upstream,
                        "domain": fid[2],
                    },
                    "bootstrap_section",
                    i,
                ),
            }
        )
    return out


def extract_standards(lines: list[str]) -> list[dict[str, Any]]:
    """标准表 -> ``standard``。

    **只收有编号的**。``PMBus`` / ``CAN FD`` 这类以名称标识的规范确实是真标准,
    但标识里没有可核对的编号; 放进初始数据后下游会把 ``std::PMBus`` 当成可引用
    的标准号, 而 §18.10 注 8 禁止编造标准条款号。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    header = re.compile(r"^\|\s*标准号\s*\|")
    in_table = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if header.match(stripped):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped.startswith("|"):
            if stripped.startswith("#"):
                in_table = False
            continue
        cells = _cells(stripped)
        if len(cells) < 2 or set(cells[0]) <= {"-", ":"}:
            continue
        sid = _clean(cells[0])
        # 判据是「标识里**有没有数字**」, 不是某个前缀形态。实测早先一版用
        # ``^[A-Z]{1,5}(?:/T)?\s?\d`` 只认 ``IEC 60664`` / ``GB 4943``, 把
        # 70 条真标准全拒了: ``IEEE C37.238`` / ``JEDEC JESD22-A104`` /
        # ``MIL-HDBK-217F`` / ``AEC-Q101`` / ``IPC-2221`` 都不匹配那个形态。
        # 「有没有编号」与「是不是标准」是两件事, 必须能区分 —— ``PMBus`` 这类
        # 以名称标识的规范确实没有编号, 但仍是真标准。
        if not sid or sid in seen or not re.search(r"\d", sid):
            continue
        # 「有数字」不足以判定是标准。实测混进三类**不是标准**的东西:
        #   - ``READ_TEMPERATURE_1`` / ``READ_FAN_SPEED_1`` / ``MFR_SPECIFIC_00..45``
        #     —— 寄存器名与占位标记, 会被当编号收下;
        #   - ``HALT（见附录F.10）`` —— 试验方法名;
        #   - ``GB/T 2423（系列）`` —— 系列标准, 没有单一编号可引。
        # 判据: 数字前必须有**标准族前缀**(字母/斜杠/连字符), 且不含中文与省略号。
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:[/ -][A-Za-z0-9.+]+)*", sid)
            or ".." in sid
            or re.search(r"[一-鿿]", sid)
        ):
            continue
        seen.add(sid)
        # 列序是「标准号 | 版本 | 名称 | 适用范围 | 关联规则/公式」。
        # 版本格可能是「无」/「年」这类占位, 那不是版本号。
        version = _clean(cells[1]) if len(cells) > 1 else None
        if version in ("无", "年", "-", "—"):
            version = None
        # 版本格有时把年份重复一遍(实测 ``GB/T 17626.2`` 的版本格是 ``2018-2018``),
        # 直接拼会得到 ``GB/T 17626.2-2018-2018`` —— 一个查不到的标准号, 而
        # 它看起来完全合法。年份**跟在标识后面**时视为重复, 去掉。
        if version and sid.endswith(version) and version.count("-") == 1:
            version = None
        # 版本格写成 ``2018-2018``(同一年重复两遍, 实测 GB/T 17626.2 等 7 条都是)
        # 时取第一段。**不能**对所有含 ``-`` 的版本做这件事: ``2005+A1:2012``
        # 是合法的修订版标识(实测 IEC 60601-1), 截断会丢掉 A1 修订。
        if version:
            dup = re.fullmatch(r"(\d{4})-(\d{4})", version)
            if dup and dup.group(1) == dup.group(2):
                version = dup.group(1)
        title = _clean(cells[2]) if len(cells) > 2 else None
        scope = _clean(cells[3]) if len(cells) > 3 else None
        bindings = _clean(cells[4]) if len(cells) > 4 else None
        # **拼接必须幂等**: 实测 sid 本身就可能已含年份(``GB/T 17626.11-2018``)
        # 而版本格又重复一遍(``2018-2018``)。去重把版本修成 ``2018`` 之后, 若
        # 无条件拼接就得到 ``GB/T 17626.11-2018-2018`` —— 一个查不到、却看起来
        # 完全合法的标准号。
        full = sid if not version else (sid if sid.endswith(version) else f"{sid}-{version}")
        out.append(
            {
                "id": f"std::{full}",
                "name": title or full,
                "type": "standard",
                "text": f"{full} {title or ''}".strip(),
                "properties": _props(
                    {
                        "standard_id": full,
                        # ``authority_ref`` 回填**它自己的标准号**: 标准条目的
                        # 身份就是标准号。不回填的后果不是「少个字段」——
                        # ``ConflictDetector.detect_value_conflicts`` 缺 ``source``
                        # 时把 document 记成 ``"unknown"``, 于是它给出的
                        # 「采用更权威的来源」没有任何依据可循, 而调用方看不出
                        # 这个建议是空转的。124 条标准里 109 条因此是「无出处」。
                        #
                        # 只回填**身份**, 不改 ``citation_status``: 条目读自方案表,
                        # 读到不等于现行有效 —— 那是 corrections.yaml 的职责。
                        "authority_ref": full,
                        "version": version,
                        "title": title,
                        "scope": scope,
                        "bindings": bindings,
                        "citation_status": "UNVERIFIED",
                    },
                    # 标准条目本身也是从方案表里读的, **读到不等于现行有效**。
                    # 默认 unverified; 只有 corrections.yaml 里查证过的(现行版号、
                    # 废止关系)才会被 _do 升级为 standard 依据。
                    "bootstrap_section",
                    i,
                ),
            }
        )
    return out


#: 勘误块: ``> **勘误 E-1**：…`` / ``> **勘误 E-4 说明**：…``
#:
#: 标签后允许有修饰词(实测 E-4 写作「E-4 说明」, 其余写作「E-1」)。早先一版
#: 写成 ``勘误\s*(E-\d+)\*\*`` —— 要求 ``**`` 紧跟编号, 于是 E-4 整条漏掉,
#: 而「6/7 条勘误」看起来仍是个合理数字, 不核对总数就发现不了。
_ERRATA = re.compile(r"^>\s*\*\*勘误\s*(E-\d+)(?:\s*[^:：*]*)?\*\*\s*[::]?\s*(.+)$")


def extract_errata(lines: list[str]) -> list[dict[str, Any]]:
    """勘误 -> ``erratum``。**正文必须进来** —— 只有标签是悬空引用。

    勘误是全套数据里最要命的一类标注: 它说的是「这条式子有个常见错法」。目标
    公式不在库里时, 这条警告就随公式一起消失, 而下游看到的仍是绿灯。
    """
    out = []
    for i, line in enumerate(lines, 1):
        m = _ERRATA.match(line.strip())
        if m is None:
            continue
        tag, text = m.group(1), m.group(2).strip()
        out.append(
            {
                "id": f"err::{tag}",
                "name": tag,
                "type": "erratum",
                "text": text,
                "properties": _props({"statement": text}, "bootstrap_section", i),
            }
        )
    return out


#: **权威术语补全**(websearch 查证, 2026-10-07)—— 补的是**抽象层级**的术语。
#:
#: 背景: 术语表里「额定」与「标称」只在**具体量纲**上出现(RATED_CAPACITY /
#: NOMINAL_CAPACITY / VOUT_NOM / COND_VIN_NOM), **没有抽象条目**。于是:
#:
#: - 有出处的部分(容量那对, ``authority_kind=standard``)明确区分了;
#: - 没出处的部分(电压那对, ``unverified``/``industry``)**互为同义词** ——
#:   ``VOUT_NOM``(额定输出电压)的 synonyms 里塞了「标称输出电压」,
#:   ``COND_VIN_NOM``(标称输入电压)的 synonyms 里塞了「额定输入」, 是反向合并。
#:
#: 这个模式本身说明当前的合并是「没查证过」的产物, 不是裁定结果。所以按
#: GB/T 2900.70-2008/IEC 60050-442:1998 补上抽象条目, 让区分有据可依。
#:
#: **依据**(红线 5: 只用标准原文, 不用二手博客):
#:
#: - **442-01-01 额定值 rated value**: 「通常是由制造商对一部件、装置或设备在
#:   **规定的工作条件下**所规定的一个量值。」[151-04-03]
#: - **442-01-04 标称值 nominal value**: 「用以**标志或识别**某一部件、装置或
#:   设备的合适的**近似**量值。」[151-04-01]
#:
#: 区别是**可操作的**, 不是文字游戏:
#:
#: | | 额定值 | 标称值 |
#: |---|---|---|
#: | 性质 | 规定工作条件下的**保证值** | 用于**标志识别**的**近似值** |
#: | 有条件 | 是(条件外不保证) | 否 |
#: | 产测判据能否直接引用 | 能 | **不能** —— 它不承诺任何性能 |
#:
#: 所以产测判据引用额定值; 标称值只用于标识与文档引用。这条区分必须在知识层
#: 落地, 否则「取额定电压」的取数逻辑会把标称值也认进来(实测 ``VOUT_NOM`` 的
#: synonyms 就含「标称输出电压」), 换一份文档就可能取到不承诺性能的值。
AUTHORITATIVE_TERMS: tuple[dict[str, Any], ...] = (
    {
        "id": "RATED_VALUE",
        "name": "额定值",
        "en": "rated value",
        "definition": (
            "通常是由制造商对一部件、装置或设备在规定的工作条件下所规定的一个量值。"
            " [GB/T 2900.70-2008/IEC 60050-442:1998 442-01-01]"
        ),
        "ie_ref": "151-04-03",
        "standard": "GB/T 2900.70-2008",
        "standard_section": "442-01-01",
        "synonyms": ["额定值", "rated value", "额定", "铭牌值"],
        "note": (
            "产测判据引用的是这个 —— 它是带条件的保证值。标称值(NOMINAL_VALUE)"
            "只用于标识识别, 不承诺性能, 不可直接当判据。"
        ),
    },
    {
        "id": "NOMINAL_VALUE",
        "name": "标称值",
        "en": "nominal value",
        "definition": (
            "用以标志或识别某一部件、装置或设备的合适的近似量值。"
            " [GB/T 2900.70-2008/IEC 60050-442:1998 442-01-04]"
        ),
        "ie_ref": "151-04-01",
        "standard": "GB/T 2900.70-2008",
        "standard_section": "442-01-04",
        "synonyms": ["标称值", "nominal value", "标称", "名义值"],
        "note": (
            "是**近似标识值**, 不承诺任何性能。不可与 RATED_VALUE 互为同义词 —— "
            "PA601 里「标称输入电压范围 100~127Vac」与「输入工作电压范围 88~290Vac」"
            "并存, 若把标称当额定, 产测激励点会取错。"
        ),
    },
    # ---- 第二批 (2026-10-07 websearch): 频率响应 / 时域响应 / 谐波 / 纹波 ----
    #
    #: 起因: 术语扫描发现 11 个电源领域基本术语**在术语表里根本没有条目**, 而其中
    #: 相位裕度/增益裕度/交越频率**有规则在用**(K-LOOP-001/002/003) —— 有规则、
    #: 无术语, 规则里引用的概念解析不到, 那三条判据的术语层是空的。
    #
    #: **``verbatim`` 这个标志为什么必须存在**: 查证时能拿到的有深浅两种 ——
    #: 条款号出现在标准的术语清单里(**条款号可信**), 与定义正文被逐字抄下来
    #: (**释义也可信**)。GB/T 2900.56 的 351-25 节有全文, 所以相位/增益裕度
    #: 那几条能逐字抄; GB/T 2900.33 的 551-20 节只检索到术语清单, 拿不到正文,
    #: 谐波那几条的释义只能由本项目撰写。不区分这两种情况, 读者会以为每条都
    #: 核对过原文 —— 那正是红线 5 要防的「看起来可追溯、实则无法复核」。
    {
        "id": "GAIN_CROSSOVER_FREQ",
        "name": "增益交越角频率",
        "en": "gain crossover (angular) frequency",
        "definition": "开环增益响应值为1处的角频率。",
        "standard": "GB/T 2900.56-2008",
        "standard_section": "351-25-04",
        "synonyms": ["增益交越角频率", "gain crossover frequency", "交越频率", "穿越频率"],
        "verbatim": True,
        "note": (
            "K-LOOP-001 的判据对象(交越频率须低于开关频率的一半)。标准用的全称是"
            "「增益交越角频率」, 规则与规格书里简写「交越频率」—— 别名收录简写, "
            "但 name 用标准全称。"
        ),
    },
    {
        "id": "PHASE_MARGIN",
        "name": "相位裕度",
        "en": "phase margin",
        "definition": "增益交越频率上的开环相位响应与一（-180°）弧度之差。",
        "standard": "GB/T 2900.56-2008",
        "standard_section": "351-25-05",
        "synonyms": ["相位裕度", "phase margin", "相角裕度"],
        "verbatim": True,
        "note": "K-LOOP-002 的判据对象。定义里的「一(−180°)弧度之差」是关键: 差值为**正**才有裕度。",
    },
    {
        "id": "PHASE_CROSSOVER_FREQ",
        "name": "相位交越角频率",
        "en": "phase crossover (angular) frequency",
        "definition": "开环相位响应为-π弧度处的最低角频率。",
        "standard": "GB/T 2900.56-2008",
        "standard_section": "351-25-06",
        "synonyms": ["相位交越角频率", "phase crossover frequency", "相位穿越频率"],
        "verbatim": True,
        "note": "增益裕度是在**这个**频率上取的 —— 两条规则/术语必须成对使用, 混用会取错点。",
    },
    {
        "id": "GAIN_MARGIN",
        "name": "增益裕度",
        "en": "gain margin",
        "definition": (
            "相位交越频率上开环增益响应的倒数值。"
            "注: 开环增益响应的对数表示中，增益裕度的值1/G可以当作相位交越频率上的负对数-lgG。"
        ),
        "standard": "GB/T 2900.56-2008",
        "standard_section": "351-25-07",
        "synonyms": ["增益裕度", "gain margin", "幅值裕度"],
        "verbatim": True,
        "note": "K-LOOP-003 的判据对象。定义是「倒数值」, 报数时别直接报成 dB —— 标准给的是比值。",
    },
    {
        "id": "FUNDAMENTAL_FREQUENCY",
        "name": "基波频率",
        "en": "fundamental frequency",
        "definition": "基波分量的频率。",
        "standard": "GB/T 2900.33-2004",
        "standard_section": "551-20-03",
        "synonyms": ["基波频率", "fundamental frequency", "基频"],
        "verbatim": True,
        "note": "谐波族一切术语的基准 —— 谐波次数是「与基波频率之比」, 没有它这族无从定义。",
    },
    {
        "id": "HARMONIC_FREQUENCY",
        "name": "谐波频率",
        "en": "harmonic frequency",
        "definition": "频率为基波频率整数倍的频率。",
        "standard": "GB/T 2900.33-2004",
        "standard_section": "551-20-05",
        "synonyms": ["谐波频率", "harmonic frequency", "n次谐波频率"],
        "verbatim": False,
    },
    {
        "id": "HARMONIC_COMPONENT",
        "name": "谐波分量",
        "en": "harmonic component",
        "definition": "周期量的傅里叶级数中次数大于1的分量。",
        "standard": "GB/T 2900.33-2004",
        "standard_section": "551-20-07",
        "synonyms": ["谐波分量", "harmonic component", "谐波"],
        "verbatim": False,
    },
    {
        "id": "HARMONIC_ORDER",
        "name": "谐波次数",
        "en": "harmonic order",
        "definition": "谐波频率与基波频率之比, 为正整数。",
        "standard": "GB/T 2900.33-2004",
        "standard_section": "551-20-09",
        "synonyms": ["谐波次数", "harmonic order", "谐波阶数", "谐波序次"],
        "verbatim": False,
        "note": "IEC 60050-161 的 EMC 部分注: 谐波次数又称谐波阶数(harmonic order) —— 两个中文名同一条, 收录避免检索分裂。",
    },
    {
        "id": "HARMONIC_CONTENT",
        "name": "谐波残量",
        "en": "harmonic content",
        "definition": "从一交变量中减去其基波分量后所得到的量。",
        "standard": "GB/T 2900.33-2004",
        "standard_section": "551-20-12",
        "synonyms": ["谐波残量", "harmonic content", "谐波含量"],
        "verbatim": False,
    },
    {
        "id": "TOTAL_HARMONIC_RATIO",
        "name": "总谐波比率",
        "en": "total harmonic ratio",
        "definition": (
            "总谐波比率(THD)—— 全部谐波分量的均方根值与基波分量均方根值之比。"
            "注: 分母取基波而非总量, 是 THD 与总失真比率(TDR)的唯一区别。"
        ),
        "standard": "GB/T 2900.33-2004",
        "standard_section": "551-20-13",
        "synonyms": [
            "总谐波比率",
            "total harmonic ratio",
            "THD",
            "总谐波失真",
            "总谐波畸变率",
        ],
        "verbatim": False,
        "note": "常见误用: 把「总谐波比率」与「总失真比率」混着报 —— 前者分母是基波, 后者是总量。",
    },
    {
        "id": "TOTAL_DISTORTION_RATIO",
        "name": "总失真比率",
        "en": "total distortion ratio",
        "definition": "全部谐波与间谐波分量的均方根值与交变量总均方根值之比。",
        "standard": "GB/T 2900.33-2004",
        "standard_section": "551-20-14",
        "synonyms": ["总失真比率", "total distortion ratio", "TDR"],
        "verbatim": False,
        "note": "含间谐波且分母是总量 —— 这两点是与 TOTAL_HARMONIC_RATIO 的区别, 判据引用时要写明用哪个。",
    },
    {
        "id": "RIPPLE",
        "name": "纹波",
        "en": "ripple",
        "definition": (
            "发生在与电网电源或某些确定的源(如斩波器)有关的频率上的，围绕被测量"
            "或供给量的一组不希望有的周期性偏移。"
            "注: 纹波是周期和(或)随机偏移(PARD)的一部分，规定条件下测定。"
        ),
        "standard": "GB/T 2900.89-2012",
        "standard_section": "312-07-02",
        "synonyms": ["纹波", "ripple"],
        "verbatim": True,
        "note": (
            "抽象父概念。下位见 VOUT_RIPPLE(输出纹波)与输入纹波 —— 定义里"
            "「与电源/斩波器有关的频率」是关键: 开关频率上的周期性偏移才是纹波, "
            "随机噪声不是(那是 NOISE, 312-07-04)。"
        ),
    },
    {
        "id": "NOISE",
        "name": "噪声",
        "en": "noise",
        "definition": "围绕被测量或供给量的非周期性偏移。",
        "standard": "GB/T 2900.89-2012",
        "standard_section": "312-07-04",
        "synonyms": ["噪声", "noise"],
        "verbatim": False,
        "note": "与 RIPPLE 的分界是**周期性**: 开关频率上的偏移是纹波, 随机偏移是噪声。",
    },
    # ---- 时域响应族 (GB/T 2900.56-2008 351-24 / 351-25) ----
    #
    #: **建立时间在标准里是两个条目**, 差别只有两个字但会导致报数不可比:
    #:
    #: - §351-24-29 建立时间 settling time —— 通用, 「至阶跃响应和其稳态值之差
    #:   保持小于瞬态值允差**的时刻**」
    #: - §351-25-02 控制建立时间 control settling time —— 控制系统的行为和特性,
    #:   「第一次返回允差带**并保持在允差带范围内**为止」
    #:
    #: 前者只要求进入允差带, 后者要求留在里面。对有振荡的响应, 两者的报数可以
    #: 差一整段振荡时间 —— 所以判据引用时必须写明是哪一条。
    {
        "id": "STEADY_STATE",
        "name": "稳态",
        "en": "steady state",
        "definition": (
            "在所有瞬态效应消失后，当所有输入变量保持恒定时系统所维持的状态。[101-14-01 MOD]"
        ),
        "standard": "GB/T 2900.56-2008",
        "standard_section": "351-24-09",
        "synonyms": ["稳态", "steady state"],
        "verbatim": True,
        "note": "判据里「稳态值/稳态精度」说的是它 —— 前提是「瞬态已消失」, 产测取样点必须落在稳态段。",
    },
    {
        "id": "SETTLING_TIME",
        "name": "建立时间",
        "en": "settling time",
        "definition": (
            "过渡过程时间。对于阶跃响应，从输入变量发生阶跃变化的时刻起，至阶跃"
            "响应和其稳态值之差保持小于瞬态值允差的时刻的持续时间间隔。"
        ),
        "standard": "GB/T 2900.56-2008",
        "standard_section": "351-24-29",
        "synonyms": ["建立时间", "settling time", "调节时间", "调整时间", "过渡过程时间"],
        "verbatim": True,
        "note": (
            "通用条目。控制系统的行为与特性另有 CONTROL_SETTLING_TIME"
            "(§351-25-02), 要求「返回允差带并保持在允差带范围内」—— 两个数"
            "可能不等, 判据引用要写明是哪一条。"
        ),
    },
    {
        "id": "CONTROL_RISE_TIME",
        "name": "控制上升时间",
        "en": "control rise time",
        "definition": (
            "在参比变量或扰动变量发生阶跃变化后，从被控变量第一次偏离其期望值"
            "附近的规定允差带开始，到被控变量第一次返回允差带为止的持续时间间隔。"
        ),
        "standard": "GB/T 2900.56-2008",
        "standard_section": "351-25-01",
        "synonyms": ["控制上升时间", "control rise time", "上升时间", "rise time"],
        "verbatim": True,
        "note": (
            "注意: 控制上升时间**不是**「从 10% 到 90%」那个常见的 10-90 定义 ——"
            "标准取的是「离开允差带到首次返回允差带」。引号引到 10-90 定义会与"
            "标准值不可比。下位: VOUT_RISE_TIME(输出电压上升时间)。"
        ),
    },
    {
        "id": "CONTROL_SETTLING_TIME",
        "name": "控制建立时间",
        "en": "control settling time",
        "definition": (
            "在参比变量或扰动变量发生阶跃变化后，从被控变量第一次偏离其期望值"
            "附近的规定允差带开始，到被控变量第一次返回允差带并保持在允差带"
            "范围内为止的持续时间间隔。"
        ),
        "standard": "GB/T 2900.56-2008",
        "standard_section": "351-25-02",
        "synonyms": ["控制建立时间", "control settling time"],
        "verbatim": True,
        "note": "与 SETTLING_TIME 的区别是「并保持在允差带范围内」—— 少了「保持」两字, 报数就不可比。",
    },
    {
        "id": "OVERSHOOT",
        "name": "超调（量）",
        "en": "overshoot",
        "definition": (
            "对于阶跃响应，为偏离输出变量最终稳态值的最大瞬时偏差，通常以最终"
            "稳态值与初始稳态值之差的百分数表示。"
        ),
        "standard": "GB/T 2900.56-2008",
        "standard_section": "351-24-30",
        "synonyms": ["超调", "超调量", "overshoot", "超调率"],
        "verbatim": True,
        "note": "抽象父概念。下位: DYNAMIC_OVERSHOOT(动态超调量) —— 定义里「以…之差的**百分数**」是分母, 报数时要说明用的哪两个值。",
    },
)

#: **互为同义词的对** —— 必须在生成阶段**摘掉**, 而不是留着让检索把它们当同一个。
#:
#: 依据同上: GB/T 2900.70 把额定值与标称值定义为两个不同条目(442-01-01 /
#: 442-01-04), 分别溯到 IEC 60050-151 的 151-04-03 / 151-04-01。
CONFLATED_SYNONYMS: tuple[tuple[str, str, str], ...] = (
    # (概念 id, 要摘掉的别名, 依据)
    ("VOUT_RATED", "标称输出电压", "GB/T 2900.70 442-01-01 vs 442-01-04 定义不同"),
    ("VOUT_RATED", "标称输出", "同上"),
    # 词序相反, 但混淆的是同一件事 —— 逐条列会漏, 所以每种词序都要在清单里。
    ("VOUT_RATED", "输出标称电压", "同上"),
    ("COND_VIN_NOM", "额定输入", "同上(反向合并)"),
)

#: **概念 id 改名** —— id 是引用键, 改名前必须确认引用面。
#:
#: ``VOUT_NOM`` -> ``VOUT_RATED``: 后缀 ``_NOM`` 意为 nominal(标称), 而该概念的
#: name 是「**额定**输出电压」, 且 PA601 规格书 4.3.2 的条目标题就是「额定输出
#: 电压」(-54V / 3.45V) —— 即 **name 是对的, id 后缀是错的**。
#:
#: 改 id 而不是改 name, 因为 id 的后缀会被人当语义读: ``VOUT_NOM`` 与
#: ``COND_VIN_NOM`` 并列时, 一个指额定一个指标称, 光看 id 无法分辨。
#:
#: 改名安全性已实测(2026-10-07): ``VOUT_NOM`` 在种子里**零关系边**(没有任何
#: 边以它为 source 或 target), 代码与配置里**零引用**, 所以改名无连带影响。
#: 若将来它有了引用, 必须同步 —— 所以这个清单要人工维护, 不自动改名。
#:
#: ## 后两条: id 与 name 语义无关(比后缀矛盾更严重)
#:
#: ``TRIPPLE_OUTPUT`` 的 name 是「输出纹波」, 而 ``TRIPPLE`` 是 ``TRIPLE`` 的
#: **拼写错误**且语义是「三倍/三重」—— 与纹波毫无关系。同理 ``DYNAMIC_RESPONSE``
#: 的 name 是「动态响应**恢复时间**」, 而 ``DYNAMIC_RESPONSE`` 读起来是「动态响应」
#: (一个过程, 不是时间)。光看 id 会以为它指动态响应曲线本身。
#:
#: 改名安全性(2026-10-07 实测):
#:
#: - ``TRIPPLE_OUTPUT``: 种子里**零关系边**; 代码引用 7 处 —— ``build_seed_data.py``
#:   的注释 1 处、``tests/test_kg_materialize.py`` 6 处(纯当示例节点 id 用, 已同步
#:   改名)。**故意不改 ``alembic/versions/0002_l0_rules.py:243``**: 那是已执行迁移
#:   里建表 SQL 的设计说明注释, 属于历史记录; 改它会让迁移文件与实际跑过的内容分叉,
#:   而「各环境迁移文件不一致」比「注释里有个旧 id」更糟。
#: - ``DYNAMIC_RESPONSE``: 种子关系边 0 条; 唯一引用是 ``data/seed/corrections.yaml``
#:   的修正条目 —— **必须保持旧 id**, 因为 :func:`apply_corrections` 在
#:   :func:`apply_concept_fixes` **之前**跑, 那时实体还叫 ``DYNAMIC_RESPONSE``;
#:   跟着改名会让修正条目找不到目标而被静默跳过(修正条目无 ``source``/``checked``
#:   时才跳过, 但目标不存在也跳 —— 一样是静默)。
#:   改名为 ``TRANSIENT_RECOVERY_TIME`` 而非 ``RECOVERY_TIME``: corrections.yaml 已把
#:   它的 name 修正为「**瞬态**恢复时间」, 出处《电子电源术语及定义 2 直流输出稳定
#:   电源术语》§3.49 —— id 要跟 name 一致。
CONCEPT_RENAMES: dict[str, str] = {
    "VOUT_NOM": "VOUT_RATED",
    "TRIPPLE_OUTPUT": "VOUT_RIPPLE",
    "DYNAMIC_RESPONSE": "TRANSIENT_RECOVERY_TIME",
}

#: **补别名 / 层级说明** —— 这些概念本身存在但检索面不完整。
#:
#: ``CAPACITY``(容量)有标准出处(YD/T 4523)却是**零别名**, 且它有两个下位概念
#: ``RATED_CAPACITY`` / ``NOMINAL_CAPACITY`` 却没有任何边连回它 —— 于是检索
#: 「容量」时它只匹配「容量」二字, 「额定容量」「标称容量」各走各的。
#: 补别名让父子在检索层可见。
CONCEPT_ALIAS_PATCH: dict[str, dict[str, Any]] = {
    "CAPACITY": {
        "synonyms": ["容量", "capacity", "额定容量", "标称容量"],
        "note": (
            "抽象父概念。下位区分见 RATED_CAPACITY(额定容量)与 NOMINAL_CAPACITY"
            "(标称容量) —— 两者定义不同(GB/T 2900.70 442-01-01 vs 442-01-04),"
            "别名里保留这两个词是为了让检索能命中父子, **不代表它们同义**。"
        ),
    },
    "DC_SECONDARY_SUPPLY": {"synonyms": ["直流二次电源", "DC secondary supply"]},
    "AC_SECONDARY_SUPPLY": {"synonyms": ["交流二次电源", "AC secondary supply"]},
    "THIRD_POWER_PORT": {"synonyms": ["三级电源端口", "third power port"]},
}


#: **公理 -> 规则的人工裁定结果**(方案 §4.7)。
#:
#: 替换掉方案 md 里的 ``R*/P*`` 编号 —— 那些记号**一个都解析不到**, 而公理
#: ``rules`` 字段过去根本不在门禁的 :data:`REF_FIELDS` 里, 所以这些悬空引用
#: 从来没被报出来过(「没有消费者的死数据」, 不是「已登记的引用」)。
#:
#: **映射依据**(红线 5: 方案 md 只是方案, 不是确切来源):
#: 只有 ``domain_rules/power/rules.yaml`` 里每条规则自己的 ``statement`` +
#: ``source``。每条 ``basis`` 必须能回答「**哪条规则的哪句话**实现了这个公理」。
#: ``R*/P*`` 编号仅作假设生成器, 不作依据。
#:
#: **为什么必须人工裁定**(不是「懒得自动化」): 种子里零层级结构 —— 关系只有
#: ``defined_by`` / ``has_theorem`` / ``applies_to_rule`` 三种, ``is_a`` /
#: ``subClassOf`` / ``parent`` 一条都没有, ``power_concept`` 全是孤立叶子。
#: subsumption 闭包是空集, 本体推理无法自动判定公理的适用范围。顺序是
#: 「先有层级(人工裁定) -> 再有推理(确定性代码)」, 不可颠倒。
#:
#: **14 条的结论**: 3 条强映射 + 1 条近似 + 1 条公式有规则无 +
#: 1 条按政策挡住的门禁 + 1 条排除(热力学) + **7 条 no_executable_counterpart**。
#:
#: ``status`` 取值:
#:
#: - ``mapped``: 规则库里确实有可判定判据承接
#: - ``approximation``: 有承接, 但**不是同一个物理量**(必须写 ``approximation``)
#: - ``formula_without_rule``: 公式在库, 规则库里无一条实现它
#: - ``no_executable_counterpart``: 找不到可执行对应(**必须写为什么**, 不接受光秃秃的「无」)
#: - ``gate_blocked_by_policy``: 门禁机制完备, 但被门禁的表按红线 4 不灌数据
#: - ``excluded``: 已裁决移出范围
AXIOM_RULE_IMPLEMENTATION: dict[str, dict[str, object]] = {
    "A-1": {
        "rules": ["K-PWR-124", "K-PWR-125"],
        "status": "approximation",
        "approximation": (
            "功率叠加形式, 非节点电流守恒。KCL 是 ΣI = 0(节点电流), 而 "
            "K-PWR-124/125 判的是各路功率之和 vs 声明总功率 —— 功率叠加只是"
            "KCL 在「各路同电压」下的推论。不当作等同。"
        ),
        "basis": (
            "K-PWR-124 statement 明写「多路输出功率守恒自检 (**KCL 的可判定"
            "形式**)」; 其 source 明写「依据 **KCL** 与功率叠加」。"
            "K-PWR-125 给同一残差加了功率预算允差判据。"
        ),
        "rejected": (
            "K-PWR-121 是 P_out ≤ P_in 的功率平衡(属 A-3), 与 A-1 的多路自洽不是同一件事。"
        ),
    },
    "A-2": {
        "rules": [],
        "status": "no_executable_counterpart",
        "no_counterpart_reason": (
            "KVL(回路电压代数和为零)需要**网表拓扑** —— 节点与回路的连接关系。"
            "当前数据模型只有扁平属性(``build_ttl`` 只产 `ps:t a ps:C ; ps:k v .`), "
            "没有拓扑, 所以这不是「没找到候选」而是**原理上不可判定**。"
        ),
    },
    "A-3": {
        "rules": ["K-PWR-121", "K-PWR-002", "K-PWR-124"],
        "status": "mapped",
        "basis": (
            "K-PWR-121 statement「功率平衡自检: 任意工况下 P_out ≤ P_in"
            "(效率 ≤100%), 若实测 P_out > P_in 即为数据采集/判读错误」; "
            "其 source 明写「**能量守恒**」。K-PWR-002 给出损耗定义式 "
            "Loss = Pin − Pout, K-PWR-124/125 给出多路分解下的同一守恒。"
        ),
    },
    "A-4": {
        "rules": [],
        "status": "no_executable_counterpart",
        "no_counterpart_reason": (
            "伏秒平衡(导通伏秒 = 关断伏秒 × N)在规则库里**不存在**。"
            "唯一沾边的 K-PWR-115 是保持时间的电容 sizing「C ≥ 2·P_out·t_hold / "
            "(η·(V₁² − V_end²))」, 判据是**储能电压平方**, 与电感磁通平衡"
            "是不同物理。原方案 md 把 K-PWR-101 当候选也是错的 —— 那是"
            "**电容**电荷平衡, 属 A-5。"
        ),
    },
    "A-5": {
        "rules": ["K-PWR-101", "K-PWR-103", "K-PWR-115"],
        "status": "mapped",
        "basis": (
            "K-PWR-101 statement「降压变换器输出纹波 = 电容纹波 "
            "ΔV_cap = ΔI_L/(8·f_sw·C_out) + ESR 纹波」, 即 ΔQ = ΔI_L/f_sw "
            "再由 C = ΔQ/ΔV 展开, 正是电容电荷平衡。K-PWR-103 约束纹波电流"
            "额定值(超出则电容自热), K-PWR-115 约束维持该电荷所需的储能电容。"
        ),
    },
    "A-6": {
        "rules": [],
        "status": "no_executable_counterpart",
        "no_counterpart_reason": (
            "对偶(戴维南 <-> 诺顿)是**数学构造技巧**, 不是可判定的物理约束 —— "
            "它不对应任何一条能拿实测值判真假的产测判据。原 ``rules`` 本就为空。"
        ),
    },
    "A-7": {
        "rules": [],
        "status": "no_executable_counterpart",
        "no_counterpart_reason": (
            "唯一候选 K-TLM-112 里的「地址唯一性」是 **RS485 协议层地址去重**"
            "(句中同句还有帧校验通过率、CAN 误码率), 与数学上的唯一性定理"
            "(解的存在与唯一, 如非线性方程多解判别)不是一回事。"
        ),
    },
    "A-8": {
        "rules": [],
        "status": "formula_without_rule",
        "no_counterpart_reason": (
            "公式在库、规则库里无一条实现它: F_W.9.1_THD 给出 "
            "THD = √(Σ_{n≥2} X_n²)/X_1, 而规则库检索不到任何计算 THD 的判据。"
            "唯一提到谐波的 K-MSR-102 是 ADC 均方根量化噪声 "
            "(LSB/√12), 谐波在那里只是「采样时钟与信号成谐波关系会放大噪声」"
            "的前提条件, 不是 THD 判据。补规则须先回标准原文核限值(A23)。"
        ),
    },
    "A-9": {
        "rules": ["K-TLM-102"],
        "status": "mapped",
        "basis": (
            "K-TLM-102 statement「遥测采样与带宽判据: 采样率须覆盖被测信号最高"
            "频率分量(开关纹波频率 f_sw 的 10 倍以上)或经等效低通滤波后再采样, "
            "否则高频纹波混叠为低频虚假读数」, 与 F_M.1_SAMPLING 的 "
            "f_s ≥ k·f_max (k = 5~10) 直接对应, 且给出了混叠这个失效后果。"
        ),
    },
    "A-10": {
        "rules": [],
        "status": "no_executable_counterpart",
        "no_counterpart_reason": (
            "帕塞瓦尔定理是**信号域**的能量守恒(时域能量 = 频域能量之和), 与 "
            "A-3 的**电路域**能量守恒(P_out ≤ P_in)用途不同。而信号域在规则库"
            "里一条规则都没有(见 A-8), 所以本条无处承接。"
        ),
    },
    "A-11": {
        "rules": [],
        "status": "gate_blocked_by_policy",
        "no_counterpart_reason": (
            "量纲齐次公理是**全部公式的前置门禁**, 而门禁机制四件套全在"
            "(CHECK cardinality(dimension_vec)=7 + 索引 idx_formula_dim_ok + "
            "函数 assert_formula_dimension_ok + 触发器 "
            "trg_formula_embedding_dimension_ok, 迁移已跑到 0003_l0_provenance), "
            "**被门禁的表 l0_term.formula 是 0 行**。按红线 4(不接受副本漂移/"
            "双源)该表不灌种子数据 —— 所以这不是「坏了」, 是**被政策挡住**。"
            "结论按红线 14 记: 该表须有显式的「是否灌数据」标注, 否则审计会读成"
            "「量纲门禁已实现」。"
        ),
    },
    "A-12": {
        "rules": [],
        "status": "excluded",
        "excluded_reason": "thermodynamics",
        "no_counterpart_reason": (
            "「第二定律」若指电路的诺顿定理, 对应定理应是戴维南/诺顿那条; 但它挂的"
            "是卡诺, 且 formula_refs 指向损耗预算(热阻)公式 —— 两条独立证据都"
            "指向热力学第二定律。热力学已整体移出范围(2026-10), 本条随之排除。"
        ),
    },
    "A-13": {
        "rules": [],
        "status": "no_executable_counterpart",
        "no_counterpart_reason": (
            "集总参数法是**建模前提**(把分布参数电路当集总元件), 不是可判定的"
            "物理约束 —— 它不对应任何实测判据, 违反它表现为模型不准, 而模型"
            "准不准没有判据。"
        ),
    },
    "A-14": {
        "rules": [],
        "status": "no_executable_counterpart",
        "no_counterpart_reason": (
            "周期稳态是**分析前提**, 当前所有纹波/效率规则都默认它成立, 却"
            "没有一条**检验**它。K-LOOP-001 只给出交越频率上界 fC < fSW/2, "
            "并**不声明**「已处于周期稳态」这个假设。要检验它需要一类"
            "「稳态判据」规则(例如连续两周期波形偏差), 规则库里不存在。"
        ),
    },
    "A-15": {
        "rules": [],
        "status": "no_executable_counterpart",
        "no_counterpart_reason": (
            "无源性是**系统级频响性质**, 判它需要阻抗/频响数据, 而数据模型里"
            "没有阻抗实体。原方案 md 引的两个候选在**当前**规则库里都不是"
            "无源性判据 —— K-PWR-113 是并联均流(±5% 电流分配), K-SAF-104 是 "
            "Y 电容漏电流反推容值, 都不是拓扑无源性。"
        ),
    },
}


def _apply_axiom_rule_adjudication(
    axioms: dict[str, dict[str, Any]], rels: list[dict[str, Any]]
) -> list[str]:
    """把 :data:`AXIOM_RULE_IMPLEMENTATION` 的裁定落到公理实体上。

    覆写 ``rules`` 字段(原值是方案 md 的 ``R*/P*`` 裸记号, 一个都解析不到),
    写 ``status`` 与逐条理由, 并建 ``applies_to_rule`` 边。

    **边指向 ``rule:K-*`` 而不是种子节点**: 种子不复制规则正文 —— 复制一份就是
    红线 4 的双源, 而规则会改、种子不会跟着改, 漂移只是时间问题。边按外部
    目标处理(与既有 ``qudt:`` 边同款), 解析交给门禁去 ``rules.yaml`` 核。
    """
    unmatched: list[str] = []
    for aid, adj in AXIOM_RULE_IMPLEMENTATION.items():
        node = axioms.get(aid)
        if node is None:
            unmatched.append(aid)
            continue
        props = node["properties"]
        props["rules"] = list(adj["rules"])  # type: ignore[arg-type]
        props["status"] = adj["status"]
        props["rule_adjudication"] = {
            "decided": "2026-10",
            "basis": "domain_rules/power/rules.yaml 的 statement + source",
            "note": "方案 md 的 R*/P* 编号只作假设生成器, 不作依据(红线 5)",
            "basis_detail": adj.get("basis", ""),
            "candidates_rejected": adj.get("rejected", ""),
        }
        if adj.get("approximation"):
            props["approximation"] = adj["approximation"]
        if adj.get("excluded_reason"):
            props["excluded_reason"] = adj["excluded_reason"]
        if adj.get("no_counterpart_reason"):
            props["no_counterpart_reason"] = adj["no_counterpart_reason"]
        _sync_provenance(props, "rules", props["rules"])
        # **这里不建 ``applies_to_rule`` 边**, 与方案 §4.7 的原提案不同, 理由:
        # 该节的验收写「两端都是真实节点」, 而规则节点只存在于 rules.yaml ——
        # 要让边有真实另一端就得把规则正文复制成种子实体, 那是红线 4 的双源;
        # 而 ``rule:K-*`` 这种外部目标会被 :func:`prune_non_executable` 的
        # 双端存活过滤整条丢掉(既有 ``qudt:`` 边也是这么消失的)。两条路都堵,
        # 所以公理->规则的**唯一表达是 ``rules`` 属性**, 解析交给门禁去
        # rules.yaml 核 —— 一处表达, 不可能漂移。
    return unmatched


#: **引导源里没有、但编号序列要求存在的公理**。
#:
#: A-11 量纲齐次公理在方案 md 的公理表里**没有行**, 所以 :func:`extract_axioms`
#: 抽不到它 —— 编号从 A-10 直接跳到 A-12。留这个缺口的后果是审计读到
#: 「A-1~A-15 全覆盖」时会以为量纲齐次也在库里, 而它恰恰是**全部公式的前置
#: 门禁**(方案原文「用途: 全部(前置校验)」)。
#:
#: **存在的依据只有方案 md 的原始标签**(§4.7 明确: 方案 md 可用于确定原始标签
#: 与定位, 不是实质依据 —— 红线 5)。实质内容是**物理量纲齐次性**这个公认事实,
#: 不依赖任何标准条款, 所以不需要出处, 也不该编一个。
#:
#: 它的 ``status`` 是 :data:`AXIOM_RULE_IMPLEMENTATION` 里那条
#: ``gate_blocked_by_policy`` —— 门禁机制四件套全在而 ``l0_term.formula`` 0 行,
#: 按红线 4 该表不灌种子数据, 所以门禁是被政策挡住而不是坏了。
_EXTRA_AXIOMS: tuple[dict[str, Any], ...] = (
    {
        "id": "A-11",
        "name": "量纲齐次公理",
        "text": "任何物理等式, 两侧的量纲必须相同(齐次性); 所有经验系数、比例常数必须携带量纲",
    },
)


_AXIOM_ROW = re.compile(
    r"^\|\s*(?P<axiom_id>A-\d+)\s+(?P<axiom>[^|]+?)\s*\|"
    r"\s*(?P<theorem>T\d+[^|]*?)\s*\|"
    r"\s*(?P<formula_refs>[^|]*)\|\s*(?P<rules>[^|]*)\|\s*(?P<tests>[^|]*)\|"
)


def _split_refs(cell: str) -> list[str]:
    """``R5 通例`` / ``R1, R5`` 这类格 -> 干净的记号列表。

    只取记号本身, 丢掉中文说明 —— 留着会让关系的 target 变成 ``R5 通例`` 这种
    永远匹配不上的串。

    **空格后紧跟的说明只在「记号本身不是完整 id」时才切。** 早先一版无条件
    ``split(" ")[0]``, 于是索引表里写全的 ``F_E.1_OHM_LAW`` 被砍成 ``F_E.1``
    —— 而库里的 id 就是 ``F_E.1_OHM_LAW``, 于是这条引用永远匹配不上。
    分号两侧都是记号时更明显: ``F_J.3_INDUCTOR_RIPPLE`` 本身含下划线, 砍完
    就成了 ``F_J.3_INDUCTOR_RIPPLE`` 之外的那个短号, 指向另一个公式。

    判定用 ``_looks_like_token``: 记号形态是 ``字母[.数字/下划线...]``, 后面
    再跟内容说明就切, 没跟就原样保留。
    """
    out: list[str] = []
    for part in re.split(r"[,;、]", cell):
        cleaned = _clean(part) or ""
        if not cleaned:
            continue
        token = cleaned.split(" ")[0]
        if _looks_like_token(token):
            out.append(token)
    return out


#: 记号形态: ``A`` / ``A-1`` / ``F_E.1`` / ``F_E.1_OHM_LAW`` / ``R5`` / ``G.26``。
#:
#: 用于两处: :func:`_split_refs` 判定切不切说明, 以及剪枝时判定「公式归属哪个
#: W 子域」。后者曾靠 ``domain`` 字段, 但那是生成时算的 —— 用 id 前缀判更直接,
#: 且不受 ``_props`` 里字段缺失影响。
#:
#: 记号形态。**逐段都要能吃多字符** —— 记号可以以多个同类字符开头
#: (``FF_x`` / ``TT_y``), 所以首段是 ``[A-Za-z0-9_]+`` 而不是单个字母。
#:
#: 写这个正则时试过三版, 前两版都静默丢引用: ``[AFGRPETUW]([.-]...)?``
#: 只吃一个首字符, 于是 ``F_E.1`` / ``R5`` / ``P3`` 全不匹配, 公理
#: ``formula_refs`` 从 12 条掉到 1 条, 而生成器不报错、``fullmatch``
#: 只是返回 None。**改正则必须逐个试过这些短记号**。
_TOKEN_RE = re.compile(r"[AFGRPETUW][A-Za-z0-9_]*(?:[.-][A-Za-z0-9_]+)*")


def _looks_like_token(s: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(s))


def extract_axioms(lines: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """公理-定理索引 -> ``axiom`` + ``theorem`` 实体与关系。

    ``| A-1 KCL | T1 Tellegen | `F_E.1` … | R5 … | G.26 … |``

    **按 ID 形态匹配, 不按表位置** —— 早先一版按「第几张表」定位, 抓到术语表上,
    产出 192 条内容为「监督电流」「误报」的假公理。垃圾进图比缺图更难查:
    Semantica 会拿「误报是一条公理」去推理, 而且不报错。

    公理数是方案给定的(A-1 起连续), 不是「表里有几行」; 同一公理有多条定理,
    按 ``axiom_id`` 归并, 定理单独建节点。
    """
    axioms: dict[str, dict[str, Any]] = {}
    theorems: dict[str, dict[str, Any]] = {}
    rels: list[dict[str, Any]] = []
    for i, line in enumerate(lines, 1):
        m = _AXIOM_ROW.match(line.strip())
        if m is None:
            continue
        aid, label = m.group("axiom_id"), m.group("axiom").strip()
        tid, tname = m.group("theorem").split()[0], m.group("theorem").strip()
        node = axioms.setdefault(
            aid,
            {
                "id": aid,
                "name": label,
                "type": "axiom",
                "text": label,
                "properties": _props(
                    {"label": label, "theorems": [], "rules": [], "tests": [], "formula_refs": []},
                    "bootstrap_section",
                    i,
                ),
            },
        )
        props = node["properties"]
        new_theorem = False
        for key, cell in (
            # **定理记号必须带 thm:: 前缀** —— 定理节点 id 就是
            # ``thm::T1``, 而这里原来存裸号 ``T1``, 于是同一个定理在边
            # (``thm::T1``)与属性(``T1``)里是两个字符串。查引用时两个都要
            # 认, 漏一个就是漏检。
            ("theorems", [f"thm::{tid}"]),
            ("rules", _split_refs(m.group("rules"))),
            ("tests", _split_refs(m.group("tests"))),
            ("formula_refs", _split_refs(m.group("formula_refs"))),
        ):
            for token in cell:
                if token not in props[key]:
                    props[key].append(token)
                    if key == "theorems":
                        new_theorem = True
        theorems.setdefault(
            tid,
            {
                "id": f"thm::{tid}",
                "name": tname,
                "type": "theorem",
                "text": tname,
                "properties": _props({"theorem_id": tid}, "bootstrap_section", i),
            },
        )
        # **边跟属性一样要去重**: 属性层有 ``not in`` 守卫而这里原来没有,
        # 同一对 (公理, 定理) 出现在两行时就会建两条一模一样的边。
        # 下游 ContextGraph 按 (source, target) 去重, 于是图上少一条、
        # 边表多一条 —— 对不上账, 而这种差异常年被当成「图构建有 bug」。
        if new_theorem:
            rels.append(
                {"source": aid, "target": f"thm::{tid}", "type": "has_theorem", "properties": {}}
            )

    # 公理 -> 规则的人工裁定落库(见 :data:`AXIOM_RULE_IMPLEMENTATION`)。
    # 补引导源里缺失的公理(见 :data:`_EXTRA_AXIOMS`)。A-11 没有对应定理节点
    # —— 它本身就是门禁, 不挂定理。
    for extra in _EXTRA_AXIOMS:
        if extra["id"] in axioms:
            continue
        axioms[extra["id"]] = {
            "id": extra["id"],
            "name": extra["name"],
            "type": "axiom",
            "text": extra["text"],
            "properties": _props(
                {
                    "label": extra["name"],
                    "theorems": [],
                    "rules": [],
                    "tests": [],
                    "formula_refs": [],
                },
                "bootstrap_section",
                None,
            ),
        }

    unmatched = _apply_axiom_rule_adjudication(axioms, rels)
    if unmatched:
        print(f"  [警告] 公理裁定表里的 {unmatched} 在本库找不到对应公理, 已跳过")
    return list(axioms.values()) + list(theorems.values()), rels


#: 工况比例约定的出处落点。项目自定义的可信度 0.5(与
#: `CREDIBILITY_BY_AUTHORITY` 的 `project_defined` 同档): 约定
#: 不需要外部查证, 但**不能顶格** —— 顶格 1.0 会让下游把「本项目
#: 的约定」显示成「已验证的外部事实」。`corrections.yaml` 里两条
#: `load_conditions` 修正(半载/xx%载)当初写的正是
#: `confidence: 1.0`, 且 authority_kind 落成 `industry`, 一并纠正。
LOAD_CONVENTION_SOURCE = "data/seed/corrections.yaml#load_conditions"
LOAD_CONVENTION_CONFIDENCE = 0.5


def extract_load_conditions() -> list[dict[str, Any]]:
    """工况限定词 -> ``load_condition``。比例见 :data:`LOAD_CONDITIONS`。

    ``source_ref`` 刻意为 ``None``: 方案里没有「工况词比例」这个概念的出处。
    早先一版硬编码 ``V6.0§J.5``, 而 J.5 实际是「隔离型变换器基本公式」——
    一个指向无关章节的出处比没有出处更坏, 它看起来像有据可查。
    这些比例是**项目约定**, 出处是 corrections.yaml 本身。

    额外产出 ``load_ratio(X, R)`` **事实**, 供 :data:`LOAD_RULES` 的 Datalog 规则
    消费 —— 规则要的是「事实 + 规则」, 只有词表推不出任何东西。
    ``xx%载`` 的 ratio 保持 ``None``: 它是待求量, 由模式在推理时算出。
    """
    out: list[dict[str, Any]] = []
    for item in LOAD_CONDITIONS:
        out.append(
            {
                "id": f"load::{item['zh']}",
                "name": item["zh"],
                "type": "load_condition",
                "text": f"{item['zh']} {item['en']}: ratio={item['ratio']}",
                "properties": _props(
                    {
                        "en": item["en"],
                        "ratio": item["ratio"],
                        "kind": item["kind"],
                        "pattern": item.get("pattern"),
                        "note": item["note"],
                    },
                    "convention",
                    None,
                    authority_kind="project_defined",
                    authority=LOAD_CONVENTION_SOURCE,
                    confidence=LOAD_CONVENTION_CONFIDENCE,
                ),
            }
        )
        if item["ratio"] is not None:
            out.append(
                {
                    "id": f"loadratio::{_load_key(item['en'])}",
                    "name": f"load_ratio({_load_key(item['en'])}, {item['ratio']})",
                    "type": "load_ratio",
                    "text": f"{item['zh']} 是满载的 {item['ratio']:.0%}"
                    if item["ratio"] in (0.0, 0.5, 1.0)
                    else f"{item['zh']} 是满载的 {item['ratio']}",
                    "properties": _props(
                        {"load": _load_key(item["en"]), "ratio": item["ratio"]},
                        "convention",
                        None,
                        authority_kind="project_defined",
                        # 出处 = 约定本身的落点: corrections.yaml 的
                        # load_conditions 段(git 版本化、可复核)。之前留
                        # None, 于是这 17 条在知识门里算「无出处」; 说
                        # 它们「无出处」也不准确: 项目约定是有出处的,
                        # 出处就是项目自己的约定记录。
                        authority=LOAD_CONVENTION_SOURCE,
                        confidence=LOAD_CONVENTION_CONFIDENCE,
                    ),
                }
            )
    return out


def build_relationships(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从实体属性推出关系边。

    只连**同一次抽取里真实存在**的节点 —— 指向不存在节点的边会让 Semantica 的
    foundation graph 校验失败, 而悬空边比缺边更难排查。
    """
    ids = {e["id"] for e in entities}
    rels: list[dict[str, Any]] = []
    # 术语 -> 标准。**这是让图谱连起来的唯一钩子**: 只连同源实体的话, 999 个实体里
    # 964 个是孤立的, 图谱退化成一堆点。术语的**权威出处**就是把它连到标准上的
    # 那条边 —— 边本身携带语义, 不只是连线。
    for e in entities:
        aref = (e.get("properties") or {}).get("authority_ref")
        akind = (e.get("properties") or {}).get("authority_kind")
        if akind != "standard" or not aref:
            continue
        # ``authority_ref`` 是**带条号的引用**(如「GB/Z 14429-2005 2.1.3」), 直接
        # split 取第一段只会得到「GB/Z」—— 匹配不到任何标准实体, 边就静默不生成。
        # 必须先把标准号本身抽出来。
        m = _STD_ID_RE.match(aref) if isinstance(aref, str) else None
        # **归一化必须两边一致**: 实体 id 形如 ``std::GB/T 17626.5-2019``, ``GB/T``
        # 与 ``17626`` 之间**有空格**。早先一版在这里 ``.replace(" ", "")``, 只压掉了
        # 引用侧, 于是永远匹配不上, 边一条都生成不出来, 且不报错。
        head = re.sub(r"\s+", " ", m.group(0)).strip() if m else None
        target_id = f"std::{head}" if head else None
        # **排除自环**: 标准实体的 ``authority_ref`` 就是它自己的编号 ——
        # :func:`standards_add` 与 :func:`build_referenced_standards` 都这么写,
        # 于是「这条知识由哪个标准定义」这条规则作用在标准自己身上时, 就生成
        # ``std::X defined_by std::X``。不含任何信息: 下游 materialize 早就把它
        # 当自环丢弃了, 但它仍留在种子的 relationships 记录里 —— 按记录数审图的
        # 人(以及 ``knowledge_gate`` 之类数条数的脚本)会把它当成真边, 而实际建图
        # 时它根本不存在。实测 20 条, 修完降到 0。
        if target_id == e["id"]:
            continue
        if target_id and target_id in ids:
            rels.append(
                {
                    "source": e["id"],
                    "target": target_id,
                    "type": "defined_by",
                    "properties": {"clause": aref},
                }
            )

    def add(src: str, dst: str, rtype: str) -> None:
        if src in ids and dst in ids:
            rels.append({"source": src, "target": dst, "type": rtype, "properties": {}})

    for e in entities:
        props = e.get("properties") or {}
        eid = e["id"]
        if e["type"] == "power_concept" and props.get("qudt_ref"):
            # 指向**外部本体**, 不是本图节点 —— 用 IRI 形式, 不伪造本地 id
            rels.append(
                {
                    "source": eid,
                    "target": f"qudt:{props['qudt_ref']}",
                    "type": "has_unit_kind",
                    "properties": {"external": True, "ontology": "QUDT"},
                }
            )
        # **不为 formula_refs 建边**: 它已经是属性层的引用, 再建一条
        # ``applies_to_formula`` 边就是同一语义两处表达(红线 4 双源)。
        # 实测 3 条边与 3 条 formula_refs **完全一致** —— 边是属性的镜像,
        # 删掉边不丢信息, 留着则两边迟早漂移(且谁也说不清以哪个为准)。
        #
        # 公理的规则引用走 ``applies_to_rule``(见 :func:`extract_axioms`),
        # 指向**规则库**而非种子 —— 种子不复制规则正文, 也是红线 4。
        for aid in props.get("axiom_refs") or ():
            add(eid, aid, "derived_from_axiom")
    return rels


def apply_corrections(
    entities: list[dict[str, Any]], corrections: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """应用 :mod:`data/seed/corrections.yaml` 里的已查证修正。

    返回 ``(修正后的实体, 修正记录)``。修正记录会写进输出, 这样**审计能回答
    「这条知识和方案原文有什么不同、依据是什么」** —— 这正是 §18.10 注 9 要防的
    文档↔数据漂移, 而漂移在有记录时才查得出来。

    ## 纪律: 无 ``source`` 的修正不采用

    无出处的「修正」比不修正更危险: 它看起来可信, 但无法被复核, 且会随标准改版
    悄悄失效。所以缺 ``source`` 或 ``checked`` 的条目**跳过并计数**, 不静默吞掉。
    """
    if not corrections:
        return entities, []
    by_id = {e["id"]: e for e in entities}
    applied: list[dict[str, Any]] = []
    skipped: list[str] = []

    def _authority_of(entry: dict[str, Any]) -> tuple[str, str | None]:
        """决定这条修正的**权威依据**类型与出处。

        - 有 ``standard_ref`` **且它真的是标准号** -> ``standard``, 出处是标准号
        - 显式给了 ``authority_kind`` (``book`` / ``industry``) -> 用它, 出处取
          ``authority_ref`` 或 ``source`` 原文
        - 都没给 -> ``industry``: 只查到了业界来源(行业术语表/通行叫法/厂商规格书)

        ``unverified`` **不能**作为修正的默认: 修正本身就是「已查证并改过」,
        没有依据的修正应该在读取时被跳过(见下面的 source/checked 检查)。

        ``standard_ref`` 必须真的含标准号 —— 这条守卫是被实测逼出来的:
        9 条电子电源术语把 ``standard_ref`` 写成 ``3.66 reverse voltage
        protection``, 也就是**某出版物内部的条号 + 术语正文**。旧实现只判
        非空, 于是这 9 条被标成 ``authority_kind=standard`` 且
        ``authority_ref="3.66 reverse voltage protection"`` ——
        声称「有标准号依据」而实际一个标准号都没有, 条号还塞进了要求标准号的字段。
        下游拿它当认证依据会查不到任何东西, 而记录本身「看起来完全正常」。
        判不出标准号时退回 ``industry``, 出处用 ``source`` 原文(出版物名),
        条号归 ``clause``。
        """
        sref = entry.get("standard_ref")
        if sref and _STD_ID_RE.match(str(sref)):
            return "standard", sref
        kind = entry.get("authority_kind") or "industry"
        return kind, entry.get("authority_ref") or entry.get("source")

    def _do(kind: str, entry: dict[str, Any], match: str, updates: dict[str, Any]) -> None:
        # **无 source / checked 的修正必须跳过并计数**: 无出处的「修正」比不修正更
        # 危险 —— 看起来可信、无法复核、会随标准改版悄悄失效。
        if not entry.get("source") or not entry.get("checked"):
            skipped.append(f"{kind}:{entry.get('id')}")
            return
        target = by_id.get(match)
        if target is None:
            skipped.append(f"{kind}:{entry.get('id')}(目标不存在)")
            return
        before = {k: target["properties"].get(k) for k in updates}
        # 换用标准命名时, 把原文存进 ``zh_declared``: 标准名更权威, 但方案原文是
        # 换名决策的审计依据。
        if "zh" in updates and updates["zh"] != before.get("zh"):
            target["properties"].setdefault("zh_declared", before.get("zh"))
        akind, aref = _authority_of(entry)
        src = authority_ref(aref, kind=akind, confidence=entry.get("confidence"))
        src["metadata"]["checked"] = entry["checked"]
        src["metadata"]["correction_source"] = entry["source"]
        target["properties"].update(updates)
        # 整条记录的权威类型随之升级 —— 有查证过的依据就不再是 unverified。
        target["properties"]["authority_kind"] = akind
        if aref:
            target["properties"]["authority_ref"] = aref
        for key, value in updates.items():
            target["properties"].setdefault("provenance", {})[key] = {
                "property_name": key,
                "value": value,
                "sources": [src],
            }
        applied.append(
            {
                "kind": kind,
                "id": entry.get("id"),
                "matched": match,
                "before": before,
                "after": updates,
                "authority_kind": akind,
                "authority_ref": aref,
                "source": entry["source"],
                "checked": entry["checked"],
                "confidence": entry.get("confidence"),
            }
        )

    for entry in corrections.get("standards") or ():
        # 标准 id 形如 ``std::GB 4943.1-2011``; 修正表按不带前缀的编号写
        _do(
            "standard",
            entry,
            f"std::{entry['id']}",
            {
                k: v
                for k, v in entry.items()
                if k not in ("id", "source", "checked", "note", "current")
            },
        )
        # ``replaced_by`` 指向的标准**必须同时建成实体**。只标 SUPERSEDED 而不建
        # 新实体, 会在 Semantica 的 foundation graph 里产生悬空边 —— 而缺一条
        # 边比多一条边更难排查(悬空引用看起来像数据不全, 实际是关系缺失)。
        current = entry.get("current")
        if current and current.get("id") and not entry.get("source"):
            skipped.append(f"standard:{entry['id']}(缺 source)")
        elif current:
            cid = f"std::{current['id']}"
            if cid not in by_id:
                by_id[cid] = {
                    "id": cid,
                    "name": current.get("title") or current["id"],
                    "type": "standard",
                    "text": f"{current['id']} {current.get('title') or ''}".strip(),
                    # 现行版是**查证过的**, 因此权威类型是 standard 且出处就是
                    # 标准号本身 —— 不再经过方案。
                    "properties": _props(
                        {
                            "standard_id": current["id"],
                            "version": current.get("version"),
                            "title": current.get("title"),
                            "status": "CURRENT",
                            "iec_equivalent": current.get("iec_equivalent"),
                        },
                        "correction",
                        None,
                        authority=current["id"],
                        authority_kind="standard",
                        confidence=entry.get("confidence"),
                    ),
                }
                entities.append(by_id[cid])
                applied.append(
                    {
                        "kind": "standard",
                        "id": current["id"],
                        "matched": cid,
                        "before": None,
                        "after": {"status": "CURRENT", "added_by": "correction"},
                        "authority_kind": "standard",
                        "authority_ref": current["id"],
                        "source": entry["source"],
                        "checked": entry["checked"],
                        "confidence": entry.get("confidence"),
                    }
                )

    for entry in corrections.get("concepts") or ():
        # ``standard_ref`` / ``authority_ref`` 不作为**属性**写进概念 —— 它们是
        # 权威依据的载体, 走 ``authority_kind`` + ``authority_ref``, 由 _do 统一
        # 处理。写两遍会导致两处不一致时无从判断哪处为准。
        _updates = {
            k: v
            for k, v in entry.items()
            if k
            not in (
                "id",
                "source",
                "checked",
                "note",
                "standard_ref",
                "authority_ref",
                "authority_kind",
            )
        }
        _do("power_concept", entry, entry["id"], _updates)

    for entry in corrections.get("formulas") or ():
        # 与 ``concepts`` 同口径, 只是实体类型不同。
        #
        # **这一段原先不存在, 而后果是 244 条公式永远只能是 unverified**:
        # ``apply_corrections`` 原本只有 standards / concepts / concepts_add /
        # standards_add / telemetry_families / symbols / load_conditions 七段,
        # **没有 formulas 段** —— 于是没有任何机制能给 ``formula`` 实体挂
        # authority_ref。之前那244 条公式带 ``unverified`` 不是「查证过但没标」,
        # 而是**根本没法标**(F_H.16.1 / F_K.3 / F_L.5 / F_M.1 / F_N.4 /
        # F_Q.1 / F_R.1 / F_S.1 / F_W.3 等十章共 244 条)。
        #
        # 口径与 ``concepts`` 保持一致: ``standard_ref`` / ``authority_ref`` /
        # ``authority_kind`` 由 :func:`_do` 统一处理, 不作为属性写两遍。
        _updates = {
            k: v
            for k, v in entry.items()
            if k
            not in (
                "id",
                "source",
                "checked",
                "note",
                "standard_ref",
                "authority_ref",
                "authority_kind",
            )
        }
        _do("formula", entry, entry["id"], _updates)

    for entry in corrections.get("entities") or ():
        # **通用段**: 按 id 找到实体, 用它自己的 ``type`` 当 kind。
        #
        # 为什么需要它: ``standards`` / ``concepts`` / ``formulas`` 三段都是按
        # 类型写死的, 于是 ``axiom`` / ``theorem`` / ``erratum`` / ``symbol`` 这几类
        # **没有任何修正入口** —— 跟公式那 244 条是同一类问题(缺机制, 不是缺数据)。
        # 公理与定理尤其需要: 它们的依据是学科本身而不是某条标准条款, 所以走
        # ``authority_kind: industry`` 才诚实(挂标准号反而是假的), 而 industry
        # 恰好不是 ``standard``, 不会建边 —— 不会往图里塞假边。
        target = by_id.get(entry["id"])
        if target is None:
            skipped.append("entity:%s(目标不存在)" % entry.get("id"))
            continue
        _updates = {
            k: v
            for k, v in entry.items()
            if k
            not in (
                "id",
                "source",
                "checked",
                "note",
                "standard_ref",
                "authority_ref",
                "authority_kind",
            )
        }
        _do(str(target.get("type") or "entity"), entry, entry["id"], _updates)

    # ---- 新增实体: 方案 md 里根本没有的概念/标准 ----------------------------
    #
    # 为什么要单独两段: 上面的 ``concepts`` 只能**改**已有实体(``_do`` 走
    # ``by_id.get``, 找不到就 skip), 只能改不能增; 而 ``standards`` 的
    # ``current`` 分支语义是「被替代版指向现行版」, 不能拿来凭空加一个
    # 全新标准。YD/T 4523《通信电源术语和定义》里的「休眠」「负载下电」
    # 「软启动」「N+X 冗余」方案 md 压根没提 —— 没有这两段就永远进不来,
    # 而「方案中术语可能不全」正是这个洞。
    #
    # 纪律与 ``_do`` 一致: 缺 ``source`` 或 ``checked`` 一律跳过并计数。
    # 无出处的「新增」比不新增更危险 —— 它看起来可信, 却无法复核。
    #
    # 段名用 ``*_add`` 而不是复用 ``concepts``: 增与改的审计含义不同,
    # ``corrections_applied`` 里分开才能一眼看出哪些知识是新增的。
    _META_KEYS = ("id", "source", "checked", "note", "clause")

    for entry in corrections.get("concepts_add") or ():
        cid = entry.get("id")
        if not entry.get("source") or not entry.get("checked"):
            skipped.append(f"concepts_add:{cid}(缺source或checked)")
            continue
        if not cid:
            skipped.append("concepts_add:<无id>")
            continue
        if cid in by_id:
            # 已存在就**不重复建**: 改名/换词请走 concepts 段, 那里会留
            # zh_declared 与 before/after 审计痕迹。这里重复建等于凭空多一个
            # 实体, 而两个实体的区别在检索时无法解释。
            skipped.append(f"concepts_add:{cid}(已存在, 改用concepts段)")
            continue
        akind, aref = _authority_of(entry)
        if akind not in AUTHORITY_KINDS:
            skipped.append(f"concepts_add:{cid}(authority_kind非法:{akind})")
            continue
        if akind in ("standard", "book") and not aref:
            # 声称来自标准/书籍却写不出是哪一份 —— 正是要防的那种无出处数据
            skipped.append(f"concepts_add:{cid}(声称{akind}但无standard_ref)")
            continue
        src = authority_ref(aref, kind=akind, confidence=entry.get("confidence"))
        src["metadata"]["checked"] = entry["checked"]
        src["metadata"]["correction_source"] = entry["source"]
        _new_props = {
            k: v
            for k, v in entry.items()
            if k not in _META_KEYS + ("standard_ref", "authority_ref", "authority_kind")
        }
        _new_props["authority_kind"] = akind
        if aref:
            _new_props["authority_ref"] = aref
        if entry.get("clause"):
            _new_props["clause"] = entry["clause"]
        # provenance: 逐属性挂出处, 审计要能回答「这个 zh 是谁说的」
        _new_props["provenance"] = {
            k: {"property_name": k, "value": v, "sources": [src]}
            for k, v in _new_props.items()
            if k not in ("authority_kind", "authority_ref")
        }
        _zh = _new_props.get("zh") or cid
        _en = _new_props.get("en") or ""
        ent = {
            "id": cid,
            "name": _zh,
            "type": "power_concept",
            "text": f"{_zh} {_en}".strip(),
            "properties": _new_props,
        }
        by_id[cid] = ent
        entities.append(ent)
        applied.append(
            {
                "kind": "power_concept",
                "id": cid,
                "action": "created",
                "matched": cid,
                "before": None,
                "after": {k: v for k, v in _new_props.items() if k != "provenance"},
                "authority_kind": akind,
                "authority_ref": aref,
                "source": entry["source"],
                "checked": entry["checked"],
                "confidence": entry.get("confidence"),
            }
        )

    for entry in corrections.get("standards_add") or ():
        sid = entry.get("id")
        if not entry.get("source") or not entry.get("checked"):
            skipped.append(f"standards_add:{sid}(缺source或checked)")
            continue
        if not sid:
            skipped.append("standards_add:<无id>")
            continue
        cid = f"std::{sid}"
        if cid in by_id:
            skipped.append(f"standards_add:{sid}(已存在, 改用standards段)")
            continue
        src = authority_ref(sid, kind="standard", confidence=entry.get("confidence"))
        src["metadata"]["checked"] = entry["checked"]
        src["metadata"]["correction_source"] = entry["source"]
        _new_props = {k: v for k, v in entry.items() if k not in _META_KEYS + ("id",)}
        _new_props["authority_kind"] = "standard"
        _new_props["authority_ref"] = sid
        _new_props["provenance"] = {
            k: {"property_name": k, "value": v, "sources": [src]}
            for k, v in _new_props.items()
            if k not in ("authority_kind", "authority_ref")
        }
        ent = {
            "id": cid,
            "name": entry.get("title") or sid,
            "type": "standard",
            "text": f"{sid} {entry.get('title', '')}".strip(),
            "properties": _new_props,
        }
        by_id[cid] = ent
        entities.append(ent)
        applied.append(
            {
                "kind": "standard",
                "id": cid,
                "action": "created",
                "matched": cid,
                "before": None,
                "after": {k: v for k, v in _new_props.items() if k != "provenance"},
                "authority_kind": "standard",
                "authority_ref": sid,
                "source": entry["source"],
                "checked": entry["checked"],
                "confidence": entry.get("confidence"),
            }
        )
    for fam in corrections.get("telemetry_families") or ():
        # 遥信/遥测/遥控用**行业通称**作 ``zh``, 标准正名另存 ``standard_term``。
        #
        # 早先一版反过来: 把 zh 换成 GB/Z 14429 的条目正名「远程信号 / 远程测量 /
        # 远程命令」, 理由是「对齐标准」。那是**搞反了** —— 标准里这两个词都有,
        # 且 2.1.2~2.1.5 明文把遥测/遥信/遥控/遥调列为**同义词**; 而工程师实际
        # 说、实际搜的是后者, 方案自己的 YX_/YC_/YK_ 前缀也正是从它们缩写来的。
        # 命名成「远程信号」之后搜「遥信」反而命中不了 —— 检索命中率被自己拉低,
        # 与「对齐业界术语」的目标正好相反。
        #
        # 「对齐标准」在这里的落点是**记录标准正名与条号**, 不是替换掉通称。
        industry = fam.get("industry_term") or fam.get("synonym") or fam["standard_term"]
        standard_term = fam["standard_term"]
        # ``authority_ref`` 必须是**标准号**, 条号归 ``clause``。
        # 早先一版写成 ``ref = fam.get("clause", "")``, 于是 31 条遥信/遥测/遥控
        # 的 ``authority_ref`` 是 "2.1.3" 这样的条号, 而真实标准号
        # (``GB/Z 14429-2005``, 就在同一条的 ``source`` 里) 从未被使用。
        # 后果是这批数据声称「有标准依据」却查不到标准 —— 条号在要求标准号的字段里,
        # 而 ``clause`` 字段是空的。标准号取不到就退回 ``industry`` 并**不写**
        # ``authority_ref``: 留一个条号冒充标准号, 比承认「只有出版物出处」更糟。
        std_ids = _STD_ID_RE.findall(str(fam.get("source") or ""))
        std_id = std_ids[0] if std_ids else None
        clause = fam.get("clause") or None
        kind = "standard" if std_id else "industry"
        ref = std_id or fam.get("source")
        for cid in fam.get("concepts") or ():
            target = by_id.get(cid)
            if target is None:
                skipped.append(f"telemetry:{cid}(目标不存在)")
                continue
            old = target["properties"].get("zh")
            target["properties"]["zh_declared"] = old
            target["properties"]["zh"] = f"{industry}·{old}" if old else industry
            target["properties"]["telemetry_family"] = fam["family"]
            target["properties"]["standard_term"] = standard_term
            if clause:
                target["properties"]["clause"] = clause
            # **同义词全部记录**, 两种叫法都要能被检索命中。
            # 早先只留 `standard_term` 一个字段, 于是搜「遥信」找不到标着
            # 「远程信号」的那条 —— 命中率的损失是隐形的, 因为检索照样返回结果,
            # 只是少了一部分。
            target["properties"]["synonyms"] = [industry, standard_term]
            src = authority_ref(ref, kind=kind, confidence=fam.get("confidence"))
            # 与 _do 同口径: 把查证日期与可核对出处**写进 metadata**。少了这两项,
            # ``test_clause_numbers_only_where_a_source_was_read`` 这类「给了条号就得
            # 有核对点」的守卫就查不出这批 —— 守卫静默通过, 而这批的出处实际只在
            # ``corrections_applied`` 里, 不在记录自身。
            src["metadata"]["checked"] = fam["checked"]
            src["metadata"]["correction_source"] = fam["source"]
            target["properties"]["authority_kind"] = kind
            if std_id:
                target["properties"]["authority_ref"] = ref
            # 与 :func:`_props` 同一处修正: ``confidence`` 只进 provenance 的话,
            # ``to_seed_records`` 读到的记录级 confidence 恒为 None。这批
            # ``YC_*``/``YX_*``/``YK_*`` 遥测遥信遥调概念的 0.95 因此全丢。
            if fam.get("confidence") is not None:
                target["properties"]["confidence"] = fam.get("confidence")
            for key in ("zh", "telemetry_family", "standard_term", "synonyms"):
                target["properties"].setdefault("provenance", {})[key] = {
                    "property_name": key,
                    "value": target["properties"][key],
                    "sources": [src],
                }
            applied.append(
                {
                    "kind": "power_concept",
                    "id": cid,
                    "matched": cid,
                    "before": {"zh": old},
                    "after": {"zh": target["properties"]["zh"]},
                    "authority_kind": kind,
                    "authority_ref": ref,
                    "clause": clause,
                    "source": fam["source"],
                    "checked": fam["checked"],
                    "confidence": fam.get("confidence"),
                }
            )

    for group in corrections.get("symbols") or ():
        # 符号的术语出处是**组**的(一组符号共用一个标准的同一批词条), 不是逐条的。
        # 逐条写会把 30 多个符号抄 30 遍, 而标准不会为每个记号单列词条。
        aref = group.get("standard_ref")
        source = authority_ref(aref, kind="standard", confidence=group.get("confidence"))
        for name in group.get("symbols") or ():
            target = by_id.get(f"sym::{name}")
            if target is None:
                skipped.append(f"symbol:{name}(目标不存在)")
                continue
            target["properties"]["authority_kind"] = "standard"
            target["properties"]["authority_ref"] = aref
            # 与 :func:`_props` 同一处修正: 只进 provenance 的话记录级 confidence
            # 恒为 None(``to_seed_records`` 读 ``props.get("confidence")``)。
            if group.get("confidence") is not None:
                target["properties"]["confidence"] = group.get("confidence")
            target["properties"]["provenance"]["authority_ref"] = {
                "property_name": "authority_ref",
                "value": aref,
                "sources": [source],
            }
            applied.append(
                {
                    "kind": "symbol",
                    "id": name,
                    "matched": f"sym::{name}",
                    "before": {},
                    "after": {"authority_kind": "standard", "authority_ref": aref},
                    "source": group["source"],
                    "checked": group["checked"],
                    "confidence": group.get("confidence"),
                }
            )

    for entry in corrections.get("load_conditions") or ():
        _do(
            "load_condition",
            entry,
            f"load::{entry['id']}",
            {k: v for k, v in entry.items() if k not in ("id", "source", "checked", "note")},
        )

    if skipped:
        # **全部打印, 不截断**。早先写死 ``skipped[:4]``, 于是恰好 5 条以上跳过时
        # 多出来的永远看不见 —— 而「修正条目找不到目标」正是那种静默漂移: 条目还躺在
        # corrections.yaml 里看着正常, 每次构建都被跳过, 没人发现。实测就靠这个抓到过
        # 4 条死条目(其中 MTBF 只是被放错了段, 而 sym::MTBF 其实一直存在)。
        if len(skipped) > 20:
            shown = skipped[:20] + ["... 其余 %d 条见完整列表" % (len(skipped) - 20)]
        else:
            shown = skipped
        print(f"  [警告] {len(skipped)} 条修正缺 source/checked 或目标不存在, 已跳过: {shown}")
    return entities, applied


# =========================================================================
# 移除不可执行的设计侧知识
# =========================================================================
#
# 判据: **输入能不能绑到本系统的数据上**。
#
# 留下的是输入来自「型号规格量」或「实测采样」的那批 —— 也就是产测程序会
# 真的调用的计算。删掉的是输入只可能来自**电路拓扑参数**的那批。
#
# 为什么按这个判据删, 而不是贴个 ``design_side`` 标签留着:
# 留着的话, LLM 检索到它就会尝试绑定, 绑不上, 然后要么喂 0(造假数据),
# 要么报一堆「输入不存在」的错。删除之后「已接受 ⟺ 可执行」成为不变式,
# 代码里也不需要任何 ``design_side`` 特例分支。
#
# 判据是**可执行性**, 与来源字段无关 —— 所以这批删除不受来源审计结果影响。
EXECUTABLE_DOMAINS = frozenset(
    {
        "E",  # 电工基础: 欧姆定律等。23 条全部带量纲
        "S",  # 输入电流 / 交流有效值
        "L",  # 告警裕度 / 保护阈值序关系
        "N",  # 热阻 / 热网络 —— 温升测试直接用
        "R",  # 交叉调整率
        "M",  # 测量不确定度 / 协议
        "Q",  # 均流 / 下垂特性 —— 成品可测
        "H",  # 仪器匹配
    }
)
EXECUTABLE_W_SUBGROUPS = frozenset(
    {
        "W.3",  # 傅里叶 —— 对采样数据做纹波/谐波分析
        "W.9",  # THD / THD+N / SINAD —— 成品实测项
        "W.11",  # RMS / 整流均值 —— 直接对采样数组算
        "W.12",  # 可靠性分布 —— 由现场数据估 MTBF
    }
)

#: 落在被删域里、但**成品可测**因而豁免的判据模板。
#:
#: 这些不是计算式, 是**测量判据**: 加载阶跃测相位裕度比 45°、
#: 测 V_IL/V_IH 比规格。它们进产测执行序列, 只是没有 ``expr``。
#:
#: 原本还有一条 ``F_J.10.3_IEC61000_3_2_LIMIT``(谐波限值表, IEC 61000-3-2),
#: 已移除 —— EMC 不在本项目范围内。该条在 V6.0 里的「表达式」列写的是
#: 「**标准条文**(IEC 61000-3-2)」, 本身是一条指向标准条文的指针而非计算式,
#: 且全库零引用, 移除无连带影响。J 域因此归零(该域仅此一条公式)。
EXEMPT_FROM_PRUNE = frozenset(
    {
        "F_K.3_PHASE_MARGIN",
        "F_K.8_1_MEASUREMENT_PM",
        "F_K.3_GAIN_MARGIN",
        "F_K.8_2_MEASUREMENT_GM",
        "F_K.3.3_SETTLING_TIME",
        "F_W.6.1_LOGIC_THRESHOLD",
    }
)

#: 抽不出 ``expr`` 的记录里, 可能藏着上表的符号引用。拼起来做一次词边界匹配。
_TEXT_FIELDS = ("expr", "text", "statement", "derivation", "note")

_SYMBOL_ID_RE = re.compile(r"^sym::(.+)$")
_W_SUBGROUP_RE = re.compile(r"^F_W\.(\d+)")


#: ``expr`` 以这些词开头 = 它不是表达式, 是**指向别的公式的引用**。
#:
#: 实测只中一条: ``F_W.9.17_RMS_VS_PP`` 的 ``expr`` 是
#: ``"见 F_J.5_RIPPLE_RMS_TRI/SQ/SIN"`` —— 方案 md 自己也没给式子, 只说
#: 「见那三条」。它落在 ``W.9`` 子组里所以躲过了按 domain 的剪枝, 结果是
#: 一条**自称可执行却没有表达式**的公式留在库里, 而知识门的通用扫描会把
#: 它正文里的记号判成悬空引用(实测 ``F_J.5_RIPPLE_RMS_TRI`` 解析不到 ——
#: 真身 ``F_J.5_RIPPLE_RMS`` 存在, 但那条记号带的是变体后缀)。
#:
#: **不补式子**: 方案没写的式子, 补出来就是编的, 而编出来的式子会一路
#: 流到 ``eval``。宁可这条知识不存在。
_POINTER_EXPR_RE = re.compile(r"^\s*(见|参见|参照|详见|同上)")


#: **明确移出范围的公式** —— 与「不可执行」无关, 是**范围决定**。
#:
#: 判据与上面两套不同: :data:`EXECUTABLE_DOMAINS` 答的是「这条能不能算」,
#: 这里答的是「这条属不属于本项目要做的范围」。两者都导致公式不在库里,
#: 但只有本表是**可以被质疑的范围决策** —— 有人问「为什么没有热阻公式」,
#: 答案不是「算不出来」而是「散热不在范围内」。
#:
#: 热力学/散热(域 N 的 N.1、N.2 共 14 条)。实测这 14 条:
#: - rules.yaml **零引用** —— 没有任何规则以它们为依据
#: - 记录层**仅 1 条引用**: 公理 ``A-12`` 的 ``formula_refs`` 指向
#:   ``F_N.2_LOSS_BUDGET``。而 ``A-12``(热力学第二定律, 挂 ``thm::T13`` 卡诺)
#:   已因「热力学暂不设计」排除, 所以这条引用随之消失
#: - 不整域删的原因: 域 N 里 N.3 是降额、N.4/N.5/N.6 是可靠性(失效分布、
#:   Weibull、浴盆曲线、温度加速寿命)、N.7 是 FMEDA/PFH 功能安全 ——
#: 它们与热力学同域但不同学科, 整域删会连带删掉这些
#: - \F_N.5.3_CAP_RIPPLE_HEATING\ 经评估后一并移除: 电功率那半条
#:   (\P = I_ripple,rms²·ESR\)已由 \K-PWR-005\ 电容 ESR 损耗完整覆盖, 是冗余;
#:   温升那半条(\ΔT = P·R_θ\)只被已删除的 K-CAL-106 覆盖。移除后
#:   \sym::R_θ\ 随之级联消失 —— 它只被这一条引用, 热力学符号清零
OUT_OF_SCOPE_FORMULA_IDS = frozenset(
    {
        # N.1 热阻与热网络
        "F_N.1_THERMAL_NETWORK",
        "F_N.1.1_THERMAL_RESISTANCE",
        "F_N.1.2_SINK_RESISTANCE_MAX",
        "F_N.1.3_THERMAL_SERIES_PARALLEL",
        "F_N.1.4_THERMAL_TRANSIENT",
        "F_N.1.5_THERMAL_TAU",
        "F_N.1.6_THERMAL_SETTLE",
        "F_N.1.7_SOAK_TIME",
        "F_N.1.8_CONVECTION_H",
        "F_N.1.9_CONDUCTION_R",
        "F_N.1.10_FAN_MATCH",
        "F_N.1.11_RADIATION",
        # N.2 损耗预算
        "F_N.2_LOSS_BUDGET",
        "F_N.2.1_THERMAL_EFFICIENCY_LIMIT",
        # N.5 电容纹波发热(见上方移除理由)
        "F_N.5.3_CAP_RIPPLE_HEATING",
        # M.1 ISO 5725 计量能力(方案 §4.5, 2026-10-07 裁定「移除」)
        #
        # 为什么移除而不是「标注存档」:
        # - ISO 5725 的**限值表需购买或从官方渠道获取**, 拿不到。既然限值表拿不到,
        #   公式里的系数就是硬写出来的 —— 而硬写的系数正是「看起来有实际没有」:
        #   `F_M.1.9` 的 `r = 2.8·s_r` 里那个 2.8 正是 ISO 5725-2 的重复性限值
        #   k 因子, 它不在任何我们能引用的公开来源里。
        # - 实测这三条在种子里 `source=null` / `authority_kind=unverified`,
        #   且**零关系边、零规则实现**(没有任何 K-* 规则承接), 所以移除无连带
        #   影响 —— 它们从来没进过图, 也从来没被算过。
        # - 标存档是留一个占位, 审计会读成「量具能力已覆盖」(红线: 不接受看起来
        #   有实际没有)。**空缺要显式记在方案文档里, 而不是留在种子里。**
        "F_M.1.9_ISO5725_REPEATABILITY",
        "F_M.1.10_ISO5725_REPRODUCIBILITY",
        "F_M.1.13_CG_CGK",
        # N.4/N.5 寿命类(2026-10-07 裁定: 使用寿命相关移出范围)
        #
        # **为什么寿命移出范围而 MTBF 保留** —— 两者都是可靠性指标, 但判据不同:
        #
        # - 这三条算的是**寿命/容量随时间的变化**(中位寿命、电容可用容量、
        #   寿命终止时的剩余容量), 服务对象是**设计选型**(这颗电容能用几年),
        #   而产测对象是**一个已经造出来的电源** —— 测不出「还能用多久」。
        # - MTBF/失效分布/系列系统那几条算的是**故障率**, 服务对象是
        #   **可靠性验收与备件测算**, 那在产测范围内(FMEA/可靠性抽检要用),
        #   所以 ``F_N.4.2_MTBF`` / ``F_N.4.8_SERIES_SYSTEM`` /
        #   ``F_N.4.10_REDUNDANCY_NO_MTBF_GAIN`` / ``F_N.7.2_SIL_MTBF_DISTINCT``
        #   与 ``sym::MTBF``、``sym::λ_*`` 全部保留。
        #
        # 实测零连带(2026-10-07): 这三条在 ``domain_rules/`` ``tests/`` ``config/``
        # ``alembic/`` 里**零引用**, 种子里**零关系边**, ``rules.yaml`` 的「寿命」
        # 20 次命中全是**判据正文里的词**(如「电解电容寿命应大于…」), 不是对这三条
        # 公式的引用 —— 所以删掉不会让任何一条判据失去依据。
        "F_N.4.3_MEDIAN_LIFE",
        "F_N.5.1_CAP_LIFE",
        "F_N.5.2_CAP_END_OF_LIFE",
    }
)


#: **明确移出范围的非公式实体** —— 概念、标准。判据同
#: :data:``OUT_OF_SCOPE_FORMULA_IDS``, 只是对象不是公式。
#:
#: - ``BMS``(电池管理系统)与 ``BATTERY_CAPACITY``(蓄电池容量): 电池管理
#:   不在本项目范围(产测对象是电源产品本体)。实测这两条**零引用** ——
#:   没有任何记录、关系或规则提到它们, 移除无连带影响。
#:   注意 ``K-PROT-117``(输出反向电流/反灌保护)**保留**: 它的条件是
#:   「带电池母线**或可并联应用**的产品」, 电池只是触发条件之一, 并联应用
#:   那半是电源本体判据, 删了会丢掉真实的保护约束。
#: - ``std::JEDEC JESD22-A101``(稳态热阻测定): 热力学已移出范围(见上),
#:   这条标准是「稳态热阻」的测量方法标准。留着它等于留一个没有公式承接的
#:   热力学标准条目 —— 审计会读成「热阻测定已覆盖」。
#: - ``CONFORMAL_COATING``(三防涂覆) + ``std::IPC-2221``: **印制板设计/组装侧**,
#:   不是电源产品产测侧(2026-10-08 移出)。判据与 :data:`OUT_OF_SCOPE_FORMULA_IDS`
#:   同源 —— **能不能绑到产测流程上**:
#:
#:   涂覆影响爬电距离 → 影响耐压/漏电流判定, 但这条链要走**两步**; 而产测判耐压
#:   是按产品标准(GB 4943.1)给的限值, 涂覆的效果已经含在产品声明的绝缘要求里了,
#:   产测工程师不会拿涂层厚度去算爬电距离。对比同库真正绑得上产测的概念:
#:   ``PROT_OVP``→保护动作值扫描、``VOUT_RIPPLE``→输出项判合格、
#:   ``COND_TEMP_REF_Tc``/``MEAS_TEMPCO_CHAMBER_SWEEP``→扫温测温度系数、
#:   ``PRACT_MSA_GRR_ACCEPTANCE``/``INSTR_HIPOT_TESTER``→测量系统与耐压测试。
#:
#:   真正做涂层的是 **PCB 组装线**(针孔/火花检测那一套), 不是电源产品产测线。
#:   它挂的两条标准也都是设计侧的: ``std::IPC-2221`` 是**印制板设计**标准
#:   (Generic Standard on Printed Board Design), IEC 60664-1 的 5.1.3 给的是
#:   「允许减小间距」的设计余量。所以标准一并移出 —— 与上面 JESD22-A101 同一处置。
#:
#:   **注意别用「出度为 0」当判据**: 实测全库 596 个实体里 **325 个出度为 0**
#:   (formula 145 / power_concept 86 / standard 40 / symbol 21…), 边主要是
#:   concept→standard 而不是 concept→concept, 所以叶子是常态, ``VOUT_RATED``/
#:   ``PROT_OVP``/``PRACT_*`` 全是出度 0 的叶子。出度 0 区分不出「出范围」。
#:
#:   保留的是同源的 ``POLLUTION_DEGREE``: 它是判读耐压/漏电流**实测结果**的输入
#:   (污染等级定爬电距离 → 爬电距离定耐压限值 → 实测值对照判合格), 在产测判据链上。
OUT_OF_SCOPE_ENTITY_IDS = frozenset(
    {
        "BMS",
        "BATTERY_CAPACITY",
        "std::JEDEC JESD22-A101",
        "CONFORMAL_COATING",
        "std::IPC-2221",
    }
)


#: **移除的 unverified 概念** —— 查证后的处置(2026-10-07, websearch)。
#:
#: 只收**同名重复**这一类 —— 判据是机械可复核的「库里存在另一条同名且权威级更高
#: 的概念」, 不需要外部查证, 也不依赖任何标准条款号。
#:
#: ## 为什么是红线 4 而不是「新旧版本并存」
#:
#: ``CREDIBILITY_BY_AUTHORITY``: ``standard`` 0.9 / ``industry`` 0.6 / ``unverified``
#: **0.2**。同名而权威级不同的两条, 冲突消解里**低的那条永远输** —— 它赢不了, 又
#: 仍能被名称/别名检索命中, 于是同一条知识有两个可改的入口, 而改错的那个不会报错
#: (它在检索里照常返回, 只是可信度低)。
#:
#: 实测 6 组同名重复:
#:
#: | 移除 | 保留 | 保留方权威级 |
#: |---|---|---|
#: | POWER_FACTOR「功率因数」 | YC_PF「功率因数」 | standard(GB/Z 14429-2005) |
#: | POUT_MAX「输出功率」 | YC_POUT「输出功率」 | standard |
#: | IOUT_MAX「输出电流」 | YC_IOUT「输出电流」 | standard |
#: | SCP_PROTECT「输出短路保护」 | PROT_SCP「输出短路保护」 | industry |
#: | OTP_PROTECT「过温保护」 | PROT_OTP「过温保护」 | industry |
#: | OCP_PROTECTION「输出过流保护」 | PROT_OCP「输出过流保护」 | unverified, 但留 PROT_OCP 与 PROT_SCP/PROT_OTP/PROT_OVP 命名族一致 |
#:
#: **零引用是实测的**(2026-10-07): 关系边合计 **0**; ``domain_rules/`` 与 ``tests/``
#: 零命中; 唯一代码引用是 ``verify_joint_reasoning.py:371``(调试脚本列了
#: ``OCP_PROTECTION``)与本文件的注释 —— 两处都已同步。
#:
#: ## 刻意**不**收进这一类的
#:
#: 另有 49 条 unverified 概念经查证后保留, 因为移除它们的理由都还站不住:
#:
#: - ``EFFICIENCY``「整机效率」: ``YC_EFFICIENCY``「效率」虽是 standard 级量概念,
#:   但**整机效率**是系统边界的效率, 与器件效率不是同一个测量口径, 不算重复。
#: - ``INSTR_*`` 14 条: 仪器类术语条款号(GB/T 2900.77/.89)可查但成本高, 而
#:   「查证不动」不等于「无价值」—— 其中 3 条(示波器/电子负载/功率分析仪)在
#:   ``rules.yaml`` 里真被用到。按「无法确认即移除」一刀切会连带删掉在用的词。
#: - ``TEMP_COEFFICENT`` / ``VOUT_RISE_TIME``: 零规则引用是真, 但仍可查证
#:   (温度系数见 IEC 60050-161; 上升时间的抽象层已由 ``CONTROL_RISE_TIME``
#:   覆盖), 不该以「没查完」为名删掉。
#: - **引用废止标准本身是可以接受的**(ISO 5725 那批、``std::GB 17285-1998`` 等都在
#:   库里), 所以「废止」不构成移除理由 —— ``LIFETIME`` 是**既无现行条款可引、又无
#:   消费者**, 两项都不成立才移。
#:
#: ## 二、无标准术语依据且零消费者(1 条)
#:
#: - ``LIFETIME``「寿命」: ``data/seed/corrections.yaml`` 里**它自己的修正记录**写着
#:   「方案自写, **未查到标准术语**, 保持原样并标记低置信度」(confidence 0.5)——
#:   即人工查证环节当时就已确认查不到。此前查过 ``GB 3187-1994 §3.6 使用寿命``:
#:   条款号存在, 但**该标准已于 2009-04-01 废止**(由 GB/T 2900.13-2008 代替, 后者
#:   又被 GB/T 2900.99-2016 部分代替), 而现行版的对应条号未能核到。引用一条废止标准
#:   只为留住一个零消费者的概念, 不划算。
#:
#:   ``MTBF``「平均故障间隔时间」**保留**: 有 8 处引用(含 ``rules.yaml`` 2 次、
#:   ``config/table_schemas.yaml``、``domain_rules/power/knowledge.md``), 且已按
#:   corrections 升级为 GB/T 3187-1994 §7.2.8 出处 —— 与 LIFETIME 不同档。同族公式
#:   ``F_N.4.2_MTBF`` 等也保留(见 :data:`OUT_OF_SCOPE_FORMULA_IDS` 的说明)。
DUPLICATE_CONCEPT_IDS = frozenset(
    {
        "POWER_FACTOR",
        "POUT_MAX",
        "IOUT_MAX",
        "SCP_PROTECT",
        "OTP_PROTECT",
        "OCP_PROTECTION",
        # 二、无标准术语依据且零消费者
        "LIFETIME",
    }
)

#: **已知无法解析的短记号**, 在注记字段(``bindings``/``upstream``)里出现就摘掉。
#:
#: 为什么需要显式列出来: 这些记号在方案 md 里是**手写简写**, 从来没有对应的
#: 公式行 —— 生成器没解析到它们, 所以 :func:``_drop_short_refs`` 拿到的
#: ``dropped_formula_ids`` 里也不含它们, 记号就一直留在库里指向空处。
#: 知识门会报 ``undeclared_ref`` WARN, 但 WARN 不是修复。
#:
#: 逐条处置(都不是「漏解析」, 所以都不补公式):
#:
#: - ``F_L.5`` / ``F_L.6`` / ``F_N.7`` —— **章节指针**, 不是公式。它们的子公式
#:   都在库里(``F_L.5.1``~``F_L.5.7`` 共 7 条、``F_L.6.1``、``F_N.7.1``/``F_N.7.2``),
#:   V6.0 里写的是「见 ``F_L.5``」这种整节引用。改成指向某个具体子式会**丢范围**
#:   —— 比如 IEC 61508-2 引 ``F_L.5`` 指的是整节功能安全(硬件/架构/DC/诊断措施),
#:   不是那一条公式。
#: - ``F_L.7.2`` —— V6.0 的公式表里这一行**本身没有表达式列**(只有名称/上游/
#:   测试/依据)。补式子就是编(红线 2), 所以只摘记号。
#: - ``F_L.8.3`` —— V6.0 有内容(``I_cu >= I_k,end,max``, 断路器分断能力), 但
#:   判的是**上游配电断路器选型**, 不是被测电源; 引用方 IEC 60898-1/2、
#:   IEC 60947-2 是 customer 侧的低压断路器标准。产测对象不含断路器。
#: - ``F_P.4`` —— EMC 域(``F_P.4.1_DISTANCE_CORRECTION`` 场强距离修正、
#:   ``F_P.4.2_DBV_CONVERSION`` dBuV 换算)。EMC 已移出范围, 不再补。
#: - ``F_J.15`` —— EMC 域(域 J), 且该域**已无任何子公式**(J 域公式随 EMC
#:   移出范围清零)。它出现在某公式的 ``upstream`` 列表里, 同样无处可解析。
#: - ``F_N.4`` —— 章节指针, 与 ``F_N.7`` 同类。子公式
#:   ``F_N.4.1``~``F_N.4.10``(失效分布 / MTBF / Weibull 等 10 条)都在库里,
#:   引用方 ``std::IEC 62506``(加速试验方法)写的是整节。
UNRESOLVABLE_REF_TOKENS = frozenset(
    {
        "F_L.5",
        "F_L.6",
        "F_L.7.2",
        "F_L.8.3",
        "F_N.7",
        "F_P.4",
        "F_J.15",
        "F_N.4",
    }
)


def _is_executable_formula(e: dict[str, Any]) -> bool:
    """这条公式的输入能不能绑到型号数据或实测采样上。

    判据是「能不能算」, 不是「有没有 id」: 没有表达式的公式算不出来, 所以
    不算可执行。:data:`_POINTER_EXPR_RE` 那条是这里的第二道判据。
    """
    if e["id"] in OUT_OF_SCOPE_FORMULA_IDS:
        return False
    if e["id"] in EXEMPT_FROM_PRUNE:
        return True
    if _POINTER_EXPR_RE.match(str(e.get("properties", {}).get("expr") or "")):
        return False
    domain = str(e.get("properties", {}).get("domain") or "")
    if domain in EXECUTABLE_DOMAINS:
        return True
    if domain == "W":
        m = _W_SUBGROUP_RE.match(e["id"])
        return bool(m) and f"W.{m.group(1)}" in EXECUTABLE_W_SUBGROUPS
    return False


def _sync_provenance(props: dict[str, Any], field: str, value: Any) -> None:
    """把 provenance 镜像的值同步成存活字段的现值。

    为什么必须同步: ``provenance`` 是**记录构造时**按值快照下来的
    (``_record`` 里 ``{k: {"value": v, ...}} for k, v in out.items()``),
    之后剪枝改的是 ``props[field]``, 镜像不会跟着变。后果是同一个字段
    在两个地方说两件事 —— 实测 19 处(12 条公理的 ``formula_refs``、
    4 条标准的 ``bindings``、3 条公式的 ``upstream``), 存活值已经清空而
    provenance 还留着已删公式的 id。

    这比悬空引用更坏: 悬空引用至少能看出「这里指向空处」, 而镜像不一致
    是**看起来有依据**(provenance 有值)而**实际没有**(存活字段是空的)。
    """
    prov = props.get("provenance")
    if isinstance(prov, dict) and isinstance(prov.get(field), dict):
        prov[field]["value"] = value


def prune_dangling_refs(entities: list[dict[str, Any]]) -> dict[str, int]:
    """摘掉属性里**解析不到目标**的 `formula_refs` / `axiom_refs`。

    为什么单独一条不变式(而不是靠关系层丢): :func:uild_relationships
    的 `add()` 早就把解析不了的引用静默丢掉了 —— 图看着是干净的, 于是
    「属性里还留着 12 条坏指针」这件事没有任何地方会报错。不变式的意义
    正是: **已知的坏引用必须从存储里消失, 而不是靠下游不读它**。
    下次有公式被删或 id 改名, 这条会立刻摘掉并计数, 而不是等一年后有人
    翻到属性字段才发现。

    返回摘除计数(按字段)。
    """
    ids = {e["id"] for e in entities if e.get("id")}
    counts = {"formula_refs": 0, "axiom_refs": 0}
    for e in entities:
        props = e.get("properties") or {}
        for field in ("formula_refs", "axiom_refs"):
            refs = props.get(field)
            if not refs:
                continue
            kept = [r for r in refs if str(r) in ids]
            if len(kept) != len(refs):
                counts[field] += len(refs) - len(kept)
                props[field] = kept
                _sync_provenance(props, field, kept)
    return counts


#: 装「本库公式短记号」的注记字段。
#:
#: 标准与概念的注记写的是章节号(``bindings: "F_L.2.5、G.39"``), 本库公式 id
#: 带名字后缀(``F_L.2.5_...``) —— 这是记法差异, 所以解析时按短记号找前缀。
#: 值是**复合串**, 一个字段里既有本库记号(``F_L.5``)也有外部条款号
#: (``G.39``, 属于另一份文件的章节体系), 只有前者归本门管。
SHORT_REF_FIELDS = ("bindings", "upstream", "scope")

#: 复合串里的分隔符, 与知识门 :data:`REF_TOKEN_SPLIT` 同源。
_SHORT_REF_SPLIT_RE = re.compile(r"[、,，/;；\s]+")


def _drop_short_refs(props: dict[str, Any], dropped: set[str]) -> dict[str, int]:
    """把注记字段里指向**已剪掉公式**的短记号摘掉。

    为什么公式剪掉了还要回头改注记: 留下的记号指向空处, 而
    ``f767147`` 那次剪枝已经证明了后果 —— 5 条标准
    (``IEC 61508-2-2010`` / ``CISPR 22-2008`` / ``IEC 61000-4-2-2008`` 等)
    至今绑着 5 条已经不存在的公式(``F_L.5_*`` / ``F_P.2.1_LISN_IMPEDANCE``
    / ``F_J.9.7_EFFICIENCY`` 等)。知识门查出来报 WARN 是对的, 但 WARN 不是
    修复: 坏指针得从存储里消失, 和 :func:`prune_dangling_refs` 对
    ``formula_refs`` 的处置是同一条纪律。

    外部条款号(``G.39``)原样保留 —— 它不是本库 id, 解不开不是缺陷, 删掉
    就把「这份标准覆盖了哪些章节」这条信息一起丢了。

    返回摘除计数(按字段)。
    """
    counts: dict[str, int] = {}
    for fld in SHORT_REF_FIELDS:
        val = props.get(fld)
        if not val:
            continue
        removed = 0
        new_val: Any = val
        if isinstance(val, str):
            parts = _SHORT_REF_SPLIT_RE.split(val)
            kept = []
            for tok in parts:
                hit = tok in dropped or any(d.startswith(tok + "_") for d in dropped)
                if hit and tok:
                    removed += 1
                elif tok:
                    kept.append(tok)
            if removed:
                new_val = "、".join(kept)
        else:
            kept_list = []
            for item in val:
                tok = str(item)
                hit = tok in dropped or any(d.startswith(tok + "_") for d in dropped)
                if hit:
                    removed += 1
                else:
                    kept_list.append(item)
            if removed:
                new_val = kept_list
        if removed:
            counts[fld] = counts.get(fld, 0) + removed
            if new_val:
                props[fld] = new_val
            else:
                props.pop(fld, None)
            # provenance 是构造时的值快照, 不同步就留下已删公式的 id(见
            # :func:`_sync_provenance`)。字段整个被摘掉时镜像也要摘掉 ——
            # 留着一条「空值也有出处」会被冲突检测当成第二个来源。
            prov = props.get("provenance")
            if isinstance(prov, dict) and fld in prov:
                if new_val:
                    prov[fld]["value"] = new_val
                else:
                    prov.pop(fld, None)
    return counts


def prune_non_executable(
    entities: list[dict[str, Any]], rels: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """剔除不可执行的设计侧知识, 并连带清理由此产生的孤立项。

    返回 ``(存活实体, 存活关系, 分类计数)``。

    顺序有讲究, 不可调换:

    1. **先删公式** —— 它是唯一决定「这个符号还有没有人用」的依据。
    2. **再删符号** —— 只删「在存活记录里一个字都搜不到」的。这比「只被待删
       公式引用」更保守: 概念正文、关系属性里出现的符号也会被认作在用。
       顺带把**本来就悬空**的符号一起清了(实测 44 条), 它们是既有缺陷,
       不是这次删除造成的 —— 留着会让「符号都有引用」这个不变量永远不成立。
    3. **再删关系** —— 端点必须两边都在存活集合里。
    4. **最后删公理/定理** —— ``formula_refs`` 全部指向已删公式的才删。
       引用为空的不动: 「没有引用」不等于「引用失效」。
    """

    def props_of(e: dict[str, Any]) -> dict[str, Any]:
        return e.get("properties") or {}

    # 范围排除**先于**可执行性判定: 被移出范围的实体不该再参与
    # 「谁引用了谁」的分析, 否则已删公式留下的记号会把它也拖下水。
    _out_of_scope = _out_of_scope_static() | _out_of_scope_extra(entities)
    kept_entities = [
        e
        for e in entities
        if e.get("id") not in _out_of_scope
        and (e.get("type") != "formula" or _is_executable_formula(e))
    ]
    dropped_formula_ids = {e["id"] for e in entities} - {e["id"] for e in kept_entities}

    haystack = (
        " "
        + " ".join(str(props_of(e).get(f) or "") for e in kept_entities for f in _TEXT_FIELDS)
        + " "
    )

    def symbol_in_use(sym_id: str) -> bool:
        name = _SYMBOL_ID_RE.match(sym_id)
        if not name:
            return True
        return bool(
            re.search(
                r"(?<![A-Za-z0-9_])" + re.escape(name.group(1)) + r"(?![A-Za-z0-9_])",
                haystack,
            )
        )

    kept_entities = [
        e for e in kept_entities if e.get("type") != "symbol" or symbol_in_use(e["id"])
    ]
    dropped_symbol_ids = (
        {e["id"] for e in entities} - {e["id"] for e in kept_entities} - dropped_formula_ids
    )

    stats_short_refs: dict[str, int] = {}

    def axiom_refs_gone(e: dict[str, Any]) -> bool:
        refs = props_of(e).get("formula_refs") or []
        return bool(refs) and all(r in dropped_formula_ids for r in refs)

    # 摘掉指向已删公式的引用。不摘的话公理上留着一个指向空处的记号, 而下游
    # 无法区分「这条公理本来就没有公式支撑」与「公式 id 写错了/被删了」。
    for e in kept_entities:
        refs = props_of(e).get("formula_refs")
        if not refs:
            continue
        alive_refs = [r for r in refs if r not in dropped_formula_ids]
        if len(alive_refs) != len(refs):
            props_of(e)["formula_refs"] = alive_refs
            # 同步 provenance 镜像(见 :func:`_sync_provenance`)。少了这一步,
            # 后面 :func:`prune_dangling_refs` 会因 ``refs`` 已空而跳过, 于是
            # 公理的存活引用清空了而 provenance 还指着已删公式。
            _sync_provenance(props_of(e), "formula_refs", alive_refs)

    # 注记字段(``bindings`` / ``upstream`` / ``scope``)里的短记号同理。
    # **必须并入已删符号**: 只并公式时会漏掉指向符号的记号 —— 实测
    # ``std::JEDEC JESD22-A101`` 的 ``scope`` 是 ``'R_θjc'``, 而
    # ``sym::R_θjc`` 随热力学公式一起被剪掉了, 记号却留在库里指向空处。
    # **单独一个循环**: 上面那个 ``if not refs: continue`` 会跳过没有
    # ``formula_refs`` 的记录, 而标准记录装的是 ``bindings`` / ``scope``,
    # 根本不带 ``formula_refs`` —— 挂在那个循环里等于一次都没跑过。
    gone_refs = dropped_formula_ids | dropped_symbol_ids
    # 符号的**短记号**也要放进去: ``dropped_symbol_ids`` 装的是
    # ``sym::R_θjc``, 而注记字段里写的是 ``R_θjc`` —— 不去掉前缀就
    # 匹配不上, 记号会留在库里指向空处。
    gone_refs |= {i.split("::", 1)[1] for i in dropped_symbol_ids if i.startswith("sym::")}
    gone_refs |= UNRESOLVABLE_REF_TOKENS
    for e in kept_entities:
        short_dropped = _drop_short_refs(props_of(e), gone_refs)
        for fld, n in short_dropped.items():
            stats_short_refs[fld] = stats_short_refs.get(fld, 0) + n

    kept_entities = [
        e
        for e in kept_entities
        if e.get("type") not in ("axiom", "theorem") or not axiom_refs_gone(e)
    ]
    dropped_axiom_ids = (
        {e["id"] for e in entities}
        - {e["id"] for e in kept_entities}
        - dropped_formula_ids
        - dropped_symbol_ids
    )

    # 关系过滤**必须在公理删除之后**: ``has_theorem`` 的源端是公理, 若先算
    # ``alive`` 再删公理, 指向被删公理的边会留下来变成悬空边(实测 4 条)。
    alive = {e["id"] for e in kept_entities}
    kept_rels = [r for r in rels if r["source"] in alive and r["target"] in alive]
    dropped_rel_ids = {(r["source"], r["target"]) for r in rels} - {
        (r["source"], r["target"]) for r in kept_rels
    }

    stats = {
        "formula": len(dropped_formula_ids),
        "symbol": len(dropped_symbol_ids),
        "relationship": len(dropped_rel_ids),
        "axiom_theorem": len(dropped_axiom_ids),
        "short_ref": stats_short_refs,
    }
    return kept_entities, kept_rels, stats


def to_seed_records(
    entities: list[dict[str, Any]], relationships: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """转成 Semantica ``SeedDataManager`` 认识的**扁平记录数组**。

    契约(读 ``semantica.seed.seed_manager.create_foundation_graph`` 的源码确认):
    实体与关系在**同一个数组**里, 靠键区分 ——
    ``id``(+``entity_type``) 是实体; ``source_id`` + ``target_id`` 是关系。
    早先一版输出 ``{"entities": [...], "relationships": [...]}`` 两个并列键,
    结果 ``create_foundation_graph()`` 载入 1096 个实体、**0 条关系**而
    ``validate_quality`` 仍报 ``valid: True`` —— 静默丢关系, 不报错。

    两个必须显式处理的点:

    - ``confidence`` **缺省是 1.0**。不显式给就等于宣称「这条知识已验证」。
      引导来的方案原文**未查证**, 所以显式给 ``None`` —— 让审计看见「未验证」,
      而不是让它显示成与已验证等价。
    - ``_record_to_entity`` 把**除已知键外的全部字段**塞进 ``metadata``。
      所以属性必须**平铺在记录顶层**, 放进嵌套的 ``properties`` 会变成
      ``metadata["properties"]["zh"]``, 多一层且与其它来源的形状不一致。
    """
    records: list[dict[str, Any]] = []
    for e in entities:
        props = e.get("properties") or {}
        record: dict[str, Any] = {
            "id": e["id"],
            "name": e["name"],
            "entity_type": e["type"],
            "text": e.get("text") or e["name"],
            # 引导数据未查证: 显式 None, 不让它落到缺省 1.0
            "confidence": props.get("confidence"),
            # **冲突检测与追溯审计读的是顶层键**, 不是嵌套 provenance:
            # ``ConflictDetector.detect_value_conflicts`` 从 ``entity["source"]`` /
            # ``["section"]`` / ``["page"]`` / ``["confidence"]`` / ``["metadata"]``
            # 取出处, 缺 ``source`` 时 document 记成 ``"unknown"`` —— 于是它给出的
            # 「采用更权威的来源」这条建议**没有任何依据可循**。
            "source": props.get("authority_ref"),
            "section": None,
            "metadata": {"authority_kind": props.get("authority_kind")},
        }
        for key, value in props.items():
            if key in ("confidence",):
                continue
            record[key] = value
        records.append(record)
    for r in relationships:
        records.append(
            {
                "source_id": r["source"],
                "target_id": r["target"],
                "relationship_type": r["type"],
                "confidence": None,
                **{k: v for k, v in (r.get("properties") or {}).items()},
            }
        )
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description="抽取电源产品通用知识 -> 初始数据(一次性引导)")
    ap.add_argument(
        "--spec",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md",
    )
    ap.add_argument(
        "--out", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "seed"
    )
    ap.add_argument(
        "--corrections",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "seed" / "corrections.yaml",
        help="已查证修正表。每条须带 source 与 checked, 否则不采用。",
    )
    args = ap.parse_args()
    if not args.spec.is_file():
        print(f"方案文件不在: {args.spec}", file=sys.stderr)
        return 2

    corrections: dict[str, Any] | None = None
    if args.corrections.is_file():
        import yaml

        corrections = yaml.safe_load(args.corrections.read_text(encoding="utf-8"))
    else:
        print(f"  [警告] 修正表不存在: {args.corrections} —— 引导结果将**未经查证修正**直接入库")

    lines = args.spec.read_text(encoding="utf-8").splitlines()

    axiom_entities, axiom_rels = extract_axioms(lines)
    entities: list[dict[str, Any]] = []
    entities += extract_concepts(lines)
    entities += extract_symbols(lines)
    entities += extract_formulas(lines)
    entities += extract_standards(lines)
    entities += extract_errata(lines)
    entities += axiom_entities
    entities += extract_load_conditions()
    entities += build_authoritative_terms()
    entities += build_referenced_standards()
    entities += build_production_practice()

    deduped: dict[str, dict[str, Any]] = {}
    for e in entities:
        deduped.setdefault(e["id"], e)

    entities = list(deduped.values())
    entities, corrections_applied = apply_corrections(entities, corrections)
    for line in apply_concept_fixes(entities):
        print(f"  [术语] {line}")
    # 摘除必须在改名**之后**: 同义词清单是按新 id 写的(VOUT_RATED), 而改名先跑。
    # 顺序反了的话 strip 会按旧 id 查不到, 静默什么都不摘 —— 那正是要修的问题。
    stripped = strip_conflated_synonyms(entities)
    if stripped:
        for line in stripped:
            print(f"  [术语] 摘掉混淆同义词 {line}")

    # 剔除不可执行的设计侧知识。必须在 build_relationships **之前**:
    # 关系由存活实体重建, 指向已删公式的边就不会被造出来。
    rels = build_relationships(entities) + axiom_rels
    before_e, before_r = len(entities), len(rels)
    entities, rels, prune_stats = prune_non_executable(entities, rels)
    # 属性里的悬空记号要单独摘。关系层早就在 build_relationships 的 add()
    # 里静默丢弃了(所以图是干净的), 但实体属性 formula_refs / axiom_refs
    # 仍留着解析不了的记号 —— 实测 12 条: 6 条是剪枝删掉公式后的遗留,
    # 6 条是方案 md 里的 1990 年代短记号(F_E.1 这种)在本库 id 形态下
    # 解析不了(F_E.1_OHM_LAW)。留着等于把已知坏掉的指针存进知识库。
    dangling = prune_dangling_refs(entities)
    if dangling:
        print(
            f"  [摘除] 属性里的悬空引用: {dangling['formula_refs']} 条 formula_refs + "
            f"{dangling['axiom_refs']} 条 axiom_refs 解析不到目标, 已摘除"
        )

    # 审计轨迹里**不许有悬空声明**。``corrections_applied`` 记的是「这条知识
    # 被 corrections 建/改过」—— 目标实体已被范围排除删掉时, 那条记录就是
    # 一句没有对象的声明, 而下游测试(``test_added_concepts_declare_the_standard_
    # they_came_from``)按「声明即存在」查 ``by[a["id"]]``, 会直接 KeyError。
    # 实测: 移除 BMS / BATTERY_CAPACITY 后各留下 1~2 条。
    #
    # **两侧 id 形态不同, 必须归一**: 审计条目写裸编号(``GB 4943.1-2011``),
    # 实体 id 带前缀(``std::GB 4943.1-2011``)。直接拿审计 id 去比实体 id 会把
    # **41 条既有标准审计全判成陈旧** —— 实测踩过, ``std::GB 4943.1-2011``
    # 明明还在库里, 审计却被摘了。
    _alive_ids = {e["id"] for e in entities if e.get("id")}

    def _audit_target_alive(entry: dict[str, Any]) -> bool:
        aid = entry.get("id")
        if not aid:
            return True  # 没有 id 的条目不由本过滤判断
        return bool({str(aid), f"std::{aid}", f"sym::{aid}"} & _alive_ids)

    _stale = [a for a in corrections_applied if not _audit_target_alive(a)]
    if _stale:
        corrections_applied = [a for a in corrections_applied if _audit_target_alive(a)]
        print(
            f"  [摘除] corrections_applied 里 {len(_stale)} 条指向已移除实体的审计记录, 已摘除: "
            f"{sorted({str(a.get('id')) for a in _stale})[:6]}"
        )
    short_ref_note = (
        " / 注记短记号 " + " ".join(f"{k} {v}" for k, v in sorted(prune_stats["short_ref"].items()))
        if prune_stats["short_ref"]
        else ""
    )
    print(
        f"  [剪枝] 剔除不可执行知识: 公式 {prune_stats['formula']} / "
        f"符号 {prune_stats['symbol']} / 关系 {prune_stats['relationship']} / "
        f"公理定理 {prune_stats['axiom_theorem']}{short_ref_note} "
        f"(实体 {before_e}->{len(entities)}, 关系 {before_r}->{len(rels)})"
    )
    payload = {
        "schema_version": 1,
        "provenance": {
            "bootstrap_source": args.spec.name,
            "note": "方案 md 是**一次性引导源**, 交付后不再依赖。后续补充知识直接编辑本文件。",
            "corrections_source": args.corrections.name if args.corrections.is_file() else None,
            "corrections_applied": corrections_applied,
        },
        "records": to_seed_records(entities, rels),
        "rules": list(LOAD_RULES),
        "facts": build_facts(),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "power_domain_seed.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    counts: dict[str, int] = {}
    for e in entities:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    n_rel = sum(1 for r in payload["records"] if "source_id" in r)
    print(f"已生成 {path}")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {v:5}  {k}")
    print(f"  {n_rel:5}  relationships")
    print(f"  {len(payload['facts']):5}  Datalog facts (load_from_graph 产不出二元事实)")
    print(f"  {len(payload['records']):5}  records (Semantica 读的就是这个数组)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
