"""从方案 md 抽取「电源产品通用知识」-> Semantica / LightRAG 初始数据(一次性引导)。

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
公式连勘误), 切开就断了。LightRAG 侧读同一份 JSON 的 ``text`` 字段做图谱抽取,
所以 ``text`` 必须是**自然语言陈述**, 而不是符号串。

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
    {"zh": "满载", "en": "full load", "ratio": 1.0, "kind": "load",
     "note": "归一化基准。任何 xx%载 都相对它折算。"},
    {"zh": "半载", "en": "half load", "ratio": 0.5, "kind": "load",
     "note": "= 50%载 = 满载 × 50%。"},
    {"zh": "空载", "en": "no load", "ratio": 0.0, "kind": "load",
     "note": "输出为 0。「空载」不等于「轻载」: 空载下常降频, 损耗模型不适用同一套参数。"},
    {"zh": "最小载", "en": "minimum load", "ratio": None, "kind": "load",
     "note": "比例待确认: 最小稳定负载由具体拓扑决定, 不是常数。收录但不给比例 —— "
             "编一个数会让规则在错误工况上运行且不报错。"},
    {"zh": "额定", "en": "rated", "ratio": None, "kind": "rating",
     "note": "保证工作点/保证极限。与「标称」不同, 不可互换。不给比例。"},
    {"zh": "标称", "en": "nominal", "ratio": None, "kind": "nominal",
     "note": "常规取整的代表值, 无保证含义。不给比例。"},
    {"zh": "最大额定", "en": "absolute maximum rating", "ratio": None, "kind": "rating",
     "note": "超过即可能损坏的绝对上限, 与「额定工作值」不是一回事。"},
    {"zh": "xx%载", "en": "fraction load", "ratio": None, "kind": "load",
     "pattern": r"^(\d+(?:\.\d+)?)\s*%\s*载$",
     "note": "**按模式求值, 不是固定值**: xx%载 = 满载 × xx%。例 30%载 -> 0.30。"
             "ratio 刻意为 null —— 它是待求量, 不是常量; 给出 1.0 之类的值会让推理"
             "把任意百分比都当成满载。整串锚定以免把 P_load 的 load 误认成工况词。"},
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
    ``value_at_full_load(...)`` 是**型号数据**, 不在这里编 —— 读���具体规格书
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


#: 从 authority_ref 里抽标准号。GB/T、GB/Z、GB、IEC、YY/T 等形式。
_STD_ID_RE = re.compile(r"(?:GB/I|GB/T|GB/Z|GB|YY/T|IEC/IEC|IEC|YD/T|JB/T)\s*\d+(?:\.\d+)*(?:-\d{4})?")

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
PRODUCED_BY = (
    "bootstrap_section",
    "derived_from_id",
    "correction",
    "convention",
    "standard",
)

#: 线索类型 -> 默认权威类型。**只有 ``standard`` 与 ``convention`` 能直接推断**:
#: 从方案读到的概念一律 ``unverified`` —— 读到不等于有依据。
_PRODUCED_BY_TO_AUTHORITY = {
    "bootstrap_section": "unverified",
    "derived_from_id": "unverified",
    "correction": "standard",
    "convention": "project_defined",
    "standard": "standard",
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
    本层的职责; 概念层要的是「这条式子说了什么」, LightRAG 抽实体时看到的也应是
    工程师写的式子, 而不是被改写过的形式。

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


_AXIOM_ROW = re.compile(
    r"^\|\s*(?P<axiom_id>A-\d+)\s+(?P<axiom>[^|]+?)\s*\|"
    r"\s*(?P<theorem>T\d+[^|]*?)\s*\|"
    r"\s*(?P<formula_refs>[^|]*)\|\s*(?P<rules>[^|]*)\|\s*(?P<tests>[^|]*)\|"
)


def _split_refs(cell: str) -> list[str]:
    """``R5 通例`` / ``R1, R5`` 这类格 -> 干净的记号列表。

    只取记号本身, 丢掉中文说明 —— 留着会让关系的 target 变成 ``R5 通例`` 这种
    永远匹配不上的串。
    """
    out: list[str] = []
    for part in re.split(r"[,;、]", cell):
        token = (_clean(part) or "").split(" ")[0]
        if re.fullmatch(r"[AFGRPETUW]\.?[\w.]*", token):
            out.append(token)
    return out


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
        for key, cell in (
            ("theorems", [tid]),
            ("rules", _split_refs(m.group("rules"))),
            ("tests", _split_refs(m.group("tests"))),
            ("formula_refs", _split_refs(m.group("formula_refs"))),
        ):
            for token in cell:
                if token not in props[key]:
                    props[key].append(token)
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
        rels.append({"source": aid, "target": f"thm::{tid}", "type": "has_theorem", "properties": {}})
    return list(axioms.values()) + list(theorems.values()), rels


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
    # 术语 -> 标准。**这是让图谱连起来的��一钩子**: 只连同源实体的话, 999 个实体里
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
        m = _STD_ID_RE.search(aref) if isinstance(aref, str) else None
        # **归一化必须两边一致**: 实体 id 形如 ``std::GB/T 17626.5-2019``, ``GB/T``
        # 与 ``17626`` 之间**有空格**。早先一版在这里 ``.replace(" ", "")``, 只压掉了
        # 引用侧, 于是永远匹配不上, 边一条都生成不出来, 且不报错。
        head = re.sub(r"\s+", " ", m.group(0)).strip() if m else None
        if head and f"std::{head}" in ids:
            rels.append(
                {"source": e["id"], "target": f"std::{head}", "type": "defined_by",
                 "properties": {"clause": aref}}
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
        for ref in props.get("formula_refs") or ():
            add(eid, ref, "applies_to_formula")
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

        - 有 ``standard_ref`` -> ``standard``, 出处是标准号
        - 显式给了 ``authority_kind`` (``book`` / ``industry``) -> 用它, 出处取
          ``authority_ref`` 或 ``source`` 原文
        - 都没给 -> ``industry``: 只查到了业界来源(行业术语表/通行叫法/厂商规格书)

        ``unverified`` **不能**作为修正的默认: 修正本身就是「已查证并改过」,
        没有依据的修正应该在读取时被跳过(见下面的 source/checked 检查)。
        """
        if entry.get("standard_ref"):
            return "standard", entry["standard_ref"]
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
        _do("standard", entry, f"std::{entry['id']}", {k: v for k, v in entry.items() if k not in ("id", "source", "checked", "note", "current")})
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
            if k not in ("id", "source", "checked", "note", "standard_ref", "authority_ref", "authority_kind")
        }
        _do("power_concept", entry, entry["id"], _updates)

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
        _new_props = {
            k: v for k, v in entry.items() if k not in _META_KEYS + ("id",)
        }
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
        # 命名成「远程信号」之后搜「遥信」反而命中不了 —— 降低了 LightRAG 的
        # 命中率, 与「对齐业界术语」的目标正好相反。
        #
        # 「对齐标准」在这里的落点是**记录标准正名与条号**, 不是替换掉通称。
        industry = fam.get("industry_term") or fam.get("synonym") or fam["standard_term"]
        standard_term = fam["standard_term"]
        ref = fam.get("clause", "")
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
            # **同义词全部记录**, 两种叫法都要能被检索命中。
            # 早先只留 `standard_term` 一个字段, 于是搜「遥信」找不到标着
            # 「远程信号」的那条 —— 命中率的损失是隐形的, 因为检索照样返回结果,
            # 只是少了一部分。
            target["properties"]["synonyms"] = [industry, standard_term]
            src = authority_ref(ref, kind="standard", confidence=fam.get("confidence"))
            target["properties"]["authority_kind"] = "standard"
            target["properties"]["authority_ref"] = ref
            for key in ("zh", "telemetry_family", "standard_term", "synonyms"):
                target["properties"].setdefault("provenance", {})[key] = {
                    "property_name": key, "value": target["properties"][key], "sources": [src]
                }
            applied.append({
                "kind": "power_concept", "id": cid, "matched": cid,
                "before": {"zh": old},
                "after": {"zh": target["properties"]["zh"]}, "authority_kind": "standard", "authority_ref": ref,
                "source": fam["source"], "checked": fam["checked"],
                "confidence": fam.get("confidence"),
            })

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
        _do("load_condition", entry, f"load::{entry['id']}", {k: v for k, v in entry.items() if k not in ("id", "source", "checked", "note")})

    if skipped:
        print(f"  [警告] {len(skipped)} 条修正缺 source/checked 或目标不存在, 已跳过: {skipped[:4]}")
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
#: 这些不是计算式, 是**测量判据**或**限值表**: 加载阶跃测相位裕度比 45°、
#: 测 V_IL/V_IH 比规格、查谐波限值表。它们进产测执行序列, 只是没有 ``expr``。
EXEMPT_FROM_PRUNE = frozenset(
    {
        "F_K.3_PHASE_MARGIN",
        "F_K.8_1_MEASUREMENT_PM",
        "F_K.3_GAIN_MARGIN",
        "F_K.8_2_MEASUREMENT_GM",
        "F_K.3.3_SETTLING_TIME",
        "F_W.6.1_LOGIC_THRESHOLD",
        "F_J.10.3_IEC61000_3_2_LIMIT",
    }
)

#: 抽不出 ``expr`` 的记录里, 可能藏着上表的符号引用。拼起来做一次词边界匹配。
_TEXT_FIELDS = ("expr", "text", "statement", "derivation", "note")

_SYMBOL_ID_RE = re.compile(r"^sym::(.+)$")
_W_SUBGROUP_RE = re.compile(r"^F_W\.(\d+)")


def _is_executable_formula(e: dict[str, Any]) -> bool:
    """这条公式的输入能不能绑到型号数据或实测采样上。"""
    if e["id"] in EXEMPT_FROM_PRUNE:
        return True
    domain = str(e.get("properties", {}).get("domain") or "")
    if domain in EXECUTABLE_DOMAINS:
        return True
    if domain == "W":
        m = _W_SUBGROUP_RE.match(e["id"])
        return bool(m) and f"W.{m.group(1)}" in EXECUTABLE_W_SUBGROUPS
    return False


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

    kept_entities = [
        e
        for e in entities
        if e.get("type") != "formula" or _is_executable_formula(e)
    ]
    dropped_formula_ids = {e["id"] for e in entities} - {e["id"] for e in kept_entities}

    haystack = " " + " ".join(
        str(props_of(e).get(f) or "")
        for e in kept_entities
        for f in _TEXT_FIELDS
    ) + " "

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
        e
        for e in kept_entities
        if e.get("type") != "symbol" or symbol_in_use(e["id"])
    ]
    dropped_symbol_ids = {e["id"] for e in entities} - {e["id"] for e in kept_entities} - dropped_formula_ids

    def axiom_refs_gone(e: dict[str, Any]) -> bool:
        refs = props_of(e).get("formula_refs") or []
        return bool(refs) and all(r in dropped_formula_ids for r in refs)

    kept_entities = [
        e
        for e in kept_entities
        if e.get("type") not in ("axiom", "theorem") or not axiom_refs_gone(e)
    ]
    dropped_axiom_ids = {e["id"] for e in entities} - {e["id"] for e in kept_entities} - dropped_formula_ids - dropped_symbol_ids

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
        default=Path(__file__).resolve().parent.parent / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md",
    )
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "seed")
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


    deduped: dict[str, dict[str, Any]] = {}
    for e in entities:
        deduped.setdefault(e["id"], e)

    entities = list(deduped.values())
    entities, corrections_applied = apply_corrections(entities, corrections)

    # 剔除不可执行的设计侧知识。必须在 build_relationships **之前**:
    # 关系由存活实体重建, 指向已删公式的边就不会被造出来。
    rels = build_relationships(entities) + axiom_rels
    before_e, before_r = len(entities), len(rels)
    entities, rels, prune_stats = prune_non_executable(entities, rels)
    print(
        f"  [剪枝] 剔除不可执行知识: 公式 {prune_stats['formula']} / "
        f"符号 {prune_stats['symbol']} / 关系 {prune_stats['relationship']} / "
        f"公理定理 {prune_stats['axiom_theorem']} "
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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

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
