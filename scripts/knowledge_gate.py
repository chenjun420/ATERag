"""知识门: 种子(以及任何同形记录集)的确定性质量检查。

**为什么不用 Semantica 的 ``OntologyQualityGate``**
--------------------------------------------------
它吃的是「本体字典(类/属性)+ 实例图」, 输出 class_coverage /
property_coverage 这类指标。本项目**故意没有类/属性本体层**(不引
``OntologyEngine.from_text``、不引 LLM 本体生成), 喂进去只会得到
「你还没有类」这类通用噪音, 而真问题(引用解析不到、出处形态错、
可信度越界)一个都不会被报。门禁要对着真数据长出来, 不是对着框架的
期待长出来。

**门禁分级**
------------
``ERROR`` 退出码非零(挡住提交/部署); ``WARN`` 只报不改; ``INFO`` 是
规模数字。分级不是按「严重性直觉」, 是按**能不能机械判定**:

- 引用解析不到、id 重复、形态不对、数值越界 —— 都能机械判定, ERROR;
- 「这条没有出处」 —— 能机械判定但**不是错**(项目自定义、无出处是
  知识层的合法状态, 只影响可信度), 所以 WARN;
- 孤儿记录 —— 依赖「引用字段是否列全」这个前提, 列漏了就误报, 放 INFO。

**还有一类是「退化」而不是「缺陷」**
--------------------------------------------------
上面三类查的都是「现在有没有坏东西」。:func:`check_authority_coverage` 查的是
另一件事: **好状态有没有被悄悄退回去**。只查「坏」的话, 「全绿」既可能意味着
「干净」也可能意味着「空」—— 这两件事在门禁眼里长得一模一样, 于是改善不可见,
**回退也不可见**。这条检查把 unverified 占比 / confidence 覆盖率 / 自环数
三个比例钉成契约(见该函数的常量与注释), 达标时不制造噪声, 只在越过阈值时报 WARN。

**只用确定性判据**: 不猜、不补、不改数据。发现的问题要么在源头修,
要么显式记成已知缺口。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 声明式引用字段: 这些字段里的值**必须**是本记录集里存在的 id。
#: 显式声明而不是靠猜 —— 猜出来的引用集列漏了就成了误报源, 而误报会
#: 让门禁被忽略。
#: - ``rules``: 公理 -> 规则。**解析到 ``rules.yaml``, 不解析到种子** ——
#:   把规则正文复制成种子实体就是红线 4 的双源, 而规则会改、种子不会跟着改。
#:   过去不在这里, 所以公理 ``rules`` 里 11 个 ``R*/P*`` 裸记号(方案 md 的
#:   编号, 一个都解析不到)从来没人报过 —— 不是「已登记的引用」, 是没人看的字段。
#: - ``theorems``: 公理 -> 定理, 解析到本库 ``thm::T*`` 节点。字段值已统一带
#:   ``thm::`` 前缀(生成器 ``extract_axioms``), 与 ``has_theorem`` 边的 target
#:   同一形态 —— 以前是裸 ``T1``, 边与属性是两个字符串, 查引用要认两种写法,
#:   漏一种就是漏检。
REF_FIELDS = ("formula_refs", "axiom_refs", "rules", "theorems", "source_id", "target_id")

#: 公理 ``rules`` 字段里 ``K-*`` 记号的解析目标: **规则库**, 不是种子。
_RULE_IDS: frozenset[str] | None = None


def load_rule_ids(rules_glob: str = "domain_rules/*/rules.yaml") -> frozenset[str]:
    """规则库里的 ``K-*`` id(带缓存)。

    刻意**不**在门禁里缓存一份规则正文 —— 只缓存 id 集合, 且每次进程只读一次。
    读不到规则目录时返回空集: 那时 ``K-*`` 全部解析不到, 门禁会**报错而不是
    静默放过**, 与红线 11「依赖缺失即报错」同性质。
    """
    global _RULE_IDS
    if _RULE_IDS is not None:
        return _RULE_IDS
    import glob

    import yaml

    ids: set[str] = set()
    for path in sorted(glob.glob(rules_glob)):
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for rule in data.get("rules") or []:
            if isinstance(rule, dict) and rule.get("id"):
                ids.add(str(rule["id"]))
    _RULE_IDS = frozenset(ids)
    return _RULE_IDS

#: 本库 id 命名空间形态(用于「值形似 id」的通用扫描)。
#: 推导自真实 id 集合(有测试钉)。有了这个式子, 将来新增字段(比如某个
#: ``related_refs``)忘了在 :data:`REF_FIELDS` 登记, 通用扫描仍能抓到它指不到
#: 目标 —— 声明式是主动检查, 通用扫描是兜底。
#:
#: ``err::`` 是勘误号命名空间: 勘误 ``E-1`` 裸号与公式的章节号形态完全撞车
#: (``A-1``/``F_E.1`` 都是章节记号), 加前缀就是把勘误从章节记号里分出来。
ID_NAMESPACE = re.compile(
    r"^(F_[A-Z]|A-\d|thm::|sym::|std::|err::|load::|loadratio::|rel::)"
)

#: 已登记的**裸名 id** 类型 —— 概念名直接当 id, 不带命名空间前缀。
#:
#: **刻意不进** :data:`ID_NAMESPACE`。裸大写名与自由文本里的缩写无法区分:
#: ``BMS`` / ``RLS`` / ``Ta`` 既是合法同义词也是合法 id, 放进命名空间会让
#: 通用扫描把同义词判成悬空引用, 一次刷几百条误报 —— 而一个误报几百条的
#: 门禁等于没有门禁(本项目已经因为这个教训把 ``text`` 划进
#: :data:`EXTERNAL_FIELDS`)。
#:
#: 改成**按类型登记**: 已知形态不点名, 但条数进 :attr:`GateReport.stats`,
#: 新增第三类裸名 id 时条数会变, 门禁不会静默。已登记: 电力概念
#: (154 条 power_concept 概念名 / 遥信遥测遥代码 / SR 字段名)。
BARE_NAME_ID_TYPES = frozenset({"power_concept"})

#: 复合引用串里的分隔符: ``bindings`` 装的是 ``"F_L.2.5、G.39"``(一条标准
#: 定了两条: 公式在 G 章的条款里)。
#:
#: 不拆就会把整串当成一个值去比对, ``"F_L.2.5、G.39"`` 天然既不等于
#: ``F_L.2.5`` 也不等于任何 id, 于是每条这样的值都报一次「解析不到」。
REF_TOKEN_SPLIT = re.compile(r"[、,，/;；\s]+")

#: 标准号形态: ``GB/T 17626.5-2019`` / ``IEC 60664-1-2020`` / ``GB/Z 14429-2005``。
#: 用于「声明是 standard 却没给标准号」这类形态检查。
STANDARD_NUMBER = re.compile(r"^[A-Z]{2,4}(/([A-Z]|T|Z))?\s+\d")

#: 标准号**提取器**(与上面的形态检查不是一回事): 从 ``"GB/Z 14429-2005 §442-01-01"``
#: 里取出 ``"GB/Z 14429-2005"``, 才能拿去和库里的 ``std::`` 实体比对。
#:
#: :data:`STANDARD_NUMBER` 只锚到第一位数字(``GB/Z 1``)—— 它回答「**像不像**标准号」,
#: 提取器回答「**是哪一条**」。两者用途不同, 不能互相替代(实测踩过: 拿形态正则
#: 当提取器, 把 ``GB/Z 14429-2005`` 截成 ``GB/Z 1``, 报出 22 条假错误 ——
#: **报错的门禁比不报更糟**)。
#:
#: 三个部分各自对应标准号的一段真实形态:
#:
#: - 前缀 ``[A-Z]{2,6}(?:/[A-Z]{1,6})?``: 覆盖 ``GB/T`` / ``GB/Z`` / ``YD/T`` /
#:   ``DL/T`` / ``GJB/Z`` / ``CISPR`` / ``ISO`` / ``UL`` / ``EN`` / ``IEEE`` /
#:   ``ANSI/IEEE``。用通用形态而非枚举清单, 是为了让**没见过的族也能被提取**——
#: 少一个前缀就漏一批检查, 而漏检是静默的。
#: **必须用 :meth:`re.Pattern.match` 而不是 ``search``** —— 见
#: ``build_seed_data._STD_ID_RE`` 的同段说明: ``authority_ref`` 一律以标准号开头,
#: 而 ``search`` 会在 corrections 填进去的**查询网址**里匹配到十六进制片段
#: (``gbDetailed?id=71F772D…`` -> 把 ``71F772D`` 当标准号), 报出一堆假错误。
#: **报错的门禁比不报更糟** —— 它会让人去「修」本来没问题的东西。
#:
#: 三个部分各自对应标准号的一段真实形态:
#:
#: - 前缀 ``[A-Z]{2,6}(?:/[A-Z]{1,6})?``: 覆盖 ``GB/T`` / ``GB/Z`` / ``YD/T`` /
#:   ``DL/T`` / ``GJB/Z`` / ``CISPR`` / ``ISO`` / ``UL`` / ``EN`` / ``IEEE`` /
#:   ``ANSI/IEEE``。用通用形态而非枚举清单, 是为了让**没见过的族也能被提取**——
#:   少一个前缀就漏一批检查, 而漏检是静默的。
#: - 部分号 ``\d+[A-Za-z]?(?:[-.]\d+[A-Za-z]?)*``: 覆盖纯数字(``14429``)、
#:   带字母(``GJB/Z 299C``)、多段(``CISPR 16-1-2`` / ``ISO 13849-1-2023``)、
#:   **一位数字**(``DL/T 5-2019`` 是真实标准)。
#: - 修正件 ``\+(?:[A-Za-z]\d*(?::\d{4})?)?``: ``+A2:2013``, 也允许**裸 ``+``**
#:   (``IEC 61850-2013+`` 含后续修正件, 种子里就是这么写的)。
#:
#: **口径必须与 ``build_seed_data._STD_ID_RE`` 一致** —— 提取口径不同就会得出
#: 「库里有这个标准」与「库里没有」两个相反的结论, 而**两者都不会报错**:
#: 生成器默默少建一条 ``defined_by`` 边, 门禁默默放过一条悬空引用。
#: ``tests/test_gate_std_id_head.py`` 对真实种子的每个 ``authority_ref`` 断言
#: 两者给出同一个 head —— 那条测试就是防漂的。
#:
#: **IEC 式年份 ``IEC 60664-1:2020`` 的冒号年份已一并支持**(与
#: ``build_seed_data._STD_ID_RE`` 同步改)。只认 ``-2018`` 那类短横线年份时, 从
#: ``IEC 60664-1:2020`` 抽出的是 ``IEC 60664-1``, 与实体 id ``std::IEC 60664-1:2020``
#: 对不上 —— 生成器少建边、门禁多报一条**假**悬空引用, 而两边都不报错。
STANDARD_ID_HEAD = re.compile(
    r"[A-Z]{2,6}(?:/[A-Z]{1,6})?\s*\d+[A-Za-z]?(?:[-.]\d+[A-Za-z]?)*"
    r"(?::\d{4})?"
    r"(?:\+(?:[A-Za-z]\d*(?::\d{4})?)?)?"
)

#: 条款号形态: ``3.10`` / ``G.12`` / ``5.1.2`` / ``A-1.5``
CLAUSE_NUMBER = re.compile(r"^(\d+(\.\d+)*|[A-Z](\.\d+)+|[A-Z]-\d+(\.\d+)*)$")


@dataclass
class Finding:
    """一条门禁发现。``where`` 给人定位, ``detail`` 给机器聚合。"""

    check: str
    severity: str
    where: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "check": self.check,
            "severity": self.severity,
            "where": self.where,
            "detail": self.detail,
        }


@dataclass
class GateReport:
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def add(self, check: str, severity: str, where: str, detail: str) -> None:
        self.findings.append(Finding(check, severity, where, detail))

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.count("ERROR"),
            "warning": self.count("WARN"),
            "info": self.count("INFO"),
            "findings": [f.to_dict() for f in self.findings],
            "stats": self.stats,
        }

    def exit_code(self) -> int:
        """有 ERROR 就非零。门禁的用途是挡, 不是提醒。"""
        return 1 if self.count("ERROR") else 0


def _is_relation(rec: dict[str, Any]) -> bool:
    return bool(rec.get("source_id") and rec.get("target_id"))


def _authority_kind(rec: dict[str, Any]) -> str | None:
    """``authority_kind`` 有两个位置(领域知识放顶层, 另一些放 metadata)。"""
    meta = rec.get("metadata")
    if isinstance(meta, dict) and meta.get("authority_kind"):
        return str(meta["authority_kind"])
    val = rec.get("authority_kind")
    return str(val) if val else None


def check_ids(records: list[dict[str, Any]], report: GateReport) -> set[str]:
    """id 存在 + 不重复。返回 id 集合(供后续检查用)。"""
    seen: dict[str, int] = Counter()
    for rec in records:
        rid = rec.get("id")
        if rid is None:
            continue
        seen[str(rid)] += 1
    for rid, n in seen.items():
        if n > 1:
            report.add("id_unique", "ERROR", rid, f"id 重复 {n} 次")

    # 裸名 id(见 BARE_NAME_ID_TYPES)不计为「不匹配」, 但**必须计数**。
    # 第一版这里只报前 10 条(`stray[:10]`), 实测真种子上有 161 条失配,
    # 于是门禁显示 10 条、真实 161 条 —— 少报 151 条的「已登记形态」和
    # 少报真问题在这里是同一种静默。报总数 + 样例, 不做静默截断。
    named = [
        str(r["id"])
        for r in records
        if r.get("id")
        and not ID_NAMESPACE.match(str(r["id"]))
        and str(r.get("entity_type")) not in BARE_NAME_ID_TYPES
    ]
    if named:
        report.add(
            "id_namespace",
            "WARN",
            "-",
            f"{len(named)} 条 id 不匹配命名空间形态(通用扫描的判据因此变弱); "
            f"例: {sorted(set(named))[:5]}",
        )
    report.stats["bare_name_ids"] = sum(
        1
        for r in records
        if r.get("id")
        and not ID_NAMESPACE.match(str(r["id"]))
        and str(r.get("entity_type")) in BARE_NAME_ID_TYPES
    )
    return set(seen)


def check_refs(records: list[dict[str, Any]], ids: set[str], report: GateReport) -> None:
    """声明字段里的引用必须解析得到。

    **两个命名空间, 两套解析目标**:

    - ``thm::T*`` / ``F_*`` / ``sym::*`` 等 -> 本记录集的 id
    - ``K-*`` -> ``rules.yaml`` 的规则 id(见 :data:`REF_FIELDS`)

    分开是因为把规则复制进种子就是红线 4 的双源, 而公理确实需要指向可执行规则。
    报错信息会写明解析到哪一步失败, 否则「解析不到」分不清是种子缺节点还是
    规则库里没有这条规则 —— 而这两种错的修法完全不同。
    """
    rule_ids = load_rule_ids()
    for rec in records:
        rid = str(rec.get("id") or f"<no-id:{rec.get('entity_type')}>")
        for fld in REF_FIELDS:
            val = rec.get(fld)
            if not val:
                continue
            refs = val if isinstance(val, list) else [val]
            for ref in refs:
                ref = str(ref)
                if ref in ids:
                    continue
                if ref in rule_ids:
                    continue
                where = "种子无此节点" if not ref.startswith("K-") else "规则库无此规则"
                report.add(
                    "ref_resolves", "ERROR", rid, f"{fld} -> {ref} 解析不到({where})"
                )


#: 明确不是「指向本记录集 id」的字段, 附理由。通用扫描(WARN)跳过它们:
#:
#: - `text` / `statement` / `definition` / `note`: 自由文本, 实测
#:   公式的 `text` 就是 `"F_E.1_OHM_LAW: V = I*R"` —— id 嵌在自己的
#:   正文里是**格式设计**, 不是引用。第一版通用扫描把它当引用, 一次性
#:   报了 305 条 ERROR(其中绝大多数是这种), 而一个一次报 305 条误报的
#:   门禁等于没有门禁: 被人加进忽略清单就永久失效了。
#: - `standard_id` / `theorem_id`: 外部编号(`IEC 60664-1-2020` /
#:   `T1`), 不指向本库节点。
#: - `qudt_ref`: 外部本体(QUDT)的类名。
#: - ``approximation`` / ``no_counterpart_reason`` / ``excluded_reason`` /
#:   ``rule_adjudication``: 公理裁定理由, **散文**。它们会提到别的 id(比如
#:   「原方案把 K-PWR-101 当候选也是错的 —— 那是电容电荷平衡, 属 A-5」),
#:   但那是**叙述里的提及**, 不是引用: 值形态是自由中文, 不是 id 列表。
#:   当引用查会让通用扫描把每条理由都报一遍。
EXTERNAL_FIELDS = frozenset(
    {
        "text",
        "statement",
        "definition",
        "note",
        "standard_id",
        "theorem_id",
        "qudt_ref",
        "approximation",
        "no_counterpart_reason",
        "excluded_reason",
        "rule_adjudication",
    }
)


def _ref_tokens(val: Any) -> list[str]:
    """把一个字段值拆成待比对的记号。

    列表值逐项拆; 单值先按 :data:`REF_TOKEN_SPLIT` 拆 —— ``bindings`` 装的
    是 ``"F_L.2.5、G.39"``(一条标准定了两条), 不拆的话整串永远不等于任何
    id, 每条这样的值都会报一次「解析不到」。
    """
    out: list[str] = []
    for v in val if isinstance(val, list) else [val]:
        if not isinstance(v, str):
            continue
        out.extend(t for t in REF_TOKEN_SPLIT.split(v) if t)
    return out


def _short_ref_candidates(token: str, ids: set[str]) -> list[str]:
    """短记号 -> 全名候选。

    方案 md 引用公式时只写章节号(``F_L.2.7``), 本库 id 带名字后缀
    (``F_L.2.7_DEADBAND_MIN``)。这是**记法差异, 不是悬空引用**, 所以按
    ``token + "_"`` 前缀找候选。**候选恰好 1 个才算解析得到**: 0 个是真
    悬空(公式被剪掉了), 多个说明这条记号有歧义 —— 两种都不许蒙。
    """
    return sorted(i for i in ids if i.startswith(token + "_"))


def _is_unresolved_token(tok: str, ids: set[str]) -> bool:
    """记号是否**解析不到**。

    短记号前缀命中 **0 个**是真悬空(公式被剪掉了), 命中 **多个**是记号有
    歧义 —— 两种都算解析不到。写成「有候选就不算」会把歧义静默放过,
    而挑一条蒙过去正是这个项目反复栽跟头的地方。
    """
    if tok in ids or not ID_NAMESPACE.match(tok):
        return False
    return len(_short_ref_candidates(tok, ids)) != 1


def discover_unresolved_tokens(
    records: list[dict[str, Any]], ids: set[str]
) -> dict[str, list[str]]:
    """哪些字段里出现了**解析不到的 id 记号**, 按字段列出**不同的**记号。

    与 :func:`discover_reference_fields` 的区别只有一个: 这个给**人**看
    (到底是哪几个记号指不到东西), 那个给测试做集合断言。
    """
    hits: dict[str, set[str]] = defaultdict(set)
    for rec in records:
        if _is_relation(rec):
            continue
        for fld, val in rec.items():
            if fld == "id" or fld in EXTERNAL_FIELDS:
                continue
            for tok in _ref_tokens(val):
                if _is_unresolved_token(tok, ids):
                    hits[fld].add(tok)
    return {fld: sorted(toks) for fld, toks in hits.items()}


def discover_reference_fields(records: list[dict[str, Any]], ids: set[str]) -> dict[str, int]:
    """哪些字段里出现过**形似本库 id** 的值(按字段计数)。

    这是给**测试**用的发现器, 也是通用扫描的判据来源: 门禁本身只把
    :data:REF_FIELDS 里的悬空当 ERROR; 别的字段出现 id 记号解析不到
    时按 WARN 汇总上报, 同时这个函数让测试能断言「REF_FIELDS 已经覆盖
    全部引用字段」—— 新增一个引用字段却忘了登记, **测试会失败**,
    逼着做一次显式决定, 而不是让运行时门禁刷屏。

    返回 {字段名: 出现次数}。
    """
    hits: dict[str, int] = defaultdict(int)
    for rec in records:
        if _is_relation(rec):
            continue
        for fld, val in rec.items():
            if fld == "id" or fld in EXTERNAL_FIELDS:
                continue
            for tok in _ref_tokens(val):
                if _is_unresolved_token(tok, ids):
                    hits[fld] += 1
    return dict(hits)


def check_undeclared_id_tokens(records: list[dict[str, Any]], ids: set[str], report: GateReport) -> None:
    """未登记字段里的 id 记号解析不到 -> **WARN 汇总**, 不逐条 ERROR。

    与声明字段分开定级是实测逼出来的: 同一种「解析不到」, 出现在
    `formula_refs` 里是确定的知识缺陷(该报错), 出现在方案 md 带过来的
    `upstream` 记号里则可能是「方案用的旧编号体系」而不是缺陷(该记录
    待查)。定级一致会同时制造漏报和误报。
    """
    unresolved = discover_unresolved_tokens(records, ids)
    for fld, toks in sorted(unresolved.items(), key=lambda kv: -len(kv[1])):
        report.add(
            "undeclared_ref",
            "WARN",
            fld,
            f"{len(toks)} 种 id 记号解析不到(未登记为引用字段): {', '.join(toks[:12])}",
        )


def check_authority_shape(records: list[dict[str, Any]], report: GateReport) -> None:
    """声明 ``standard`` 的记录必须给标准号, 不是条款号。

    这条检查有前科: 曾有 31 条遥信把方案章节号(``2.1.3``)当权威出处,
    另有 9 条电子电源术语把出版物名当标准号 —— 两者都「有出处」但不可核。
    """
    for rec in records:
        rid = str(rec.get("id") or "<no-id>")
        kind = _authority_kind(rec)
        ref = rec.get("authority_ref")
        if kind == "standard":
            if not ref:
                report.add("authority_shape", "ERROR", rid, "声明 standard 但无 authority_ref")
            elif not STANDARD_NUMBER.match(str(ref)):
                report.add(
                    "authority_shape",
                    "ERROR",
                    rid,
                    f"声明 standard 但 authority_ref={ref!r} 不是标准号形态",
                )
            clause = rec.get("clause")
            if clause and not CLAUSE_NUMBER.match(str(clause)):
                report.add(
                    "clause_shape",
                    "WARN",
                    rid,
                    f"clause={clause!r} 不是条款号形态(检查是否把标准号写进了 clause)",
                )
        if rec.get("confidence") is not None:
            conf = rec.get("confidence")
            if not isinstance(conf, (int, float)) or isinstance(conf, bool):
                report.add("confidence_range", "ERROR", rid, f"confidence 非数值: {conf!r}")
            elif not 0.0 <= float(conf) <= 1.0:
                report.add("confidence_range", "ERROR", rid, f"confidence 越界: {conf}")
            elif float(conf) == 1.0:
                # 顶格不是错, 但**需要理由**。本项目已两次因顶格出问题:
                # 上游 ``track_entity`` 缺省 1.0、``ReasoningStep`` 缺省
                # 1.0; 知识侧也见过「项目约定 = 1.0」把约定显示成已验证的
                # 外部事实。所以顶格必须显式, 且要说清为什么。
                report.add(
                    "confidence_top_graded",
                    "WARN",
                    rid,
                    "confidence=1.0 顶格: 若确有理由请在 note 里写明, "
                    "否则按实际依据降档(项目约定 0.5 / 无出处 0.2)",
                )


def check_authority_refsolvable(records: list[dict[str, Any]], report: GateReport) -> None:
    """``authority_ref`` 里的标准号必须在库里真有对应的 ``standard`` 实体。

    ## 为什么这条必须存在

    :func:`check_authority_shape` 只检查「``authority_ref`` **长得像**标准号」——
    形态过了就放行。但**长得��标准号**与**库里查得到**是两件事: 实测(图诊断
    2026-10-07)有 61 处 ``authority_ref`` 引的 12 个标准号在种子里**没有实体**,
    于是 ``build_relationships`` 的 ``f"std::{head}" in ids`` 判断全部落空, 一条
    ``defined_by`` 边都建不出来, **且不报错** —— 门禁当时全绿。

    引用一个库里不存在的标准, 就是红线 5 的「看起来可追溯、实则无法复核」: 按
    authority_ref 去查标准能查到(那个标准真实存在), 但**在本项目里复核这条引用**
    无从下手, 因为库里没有它。

    ## 为什么判 WARN 而不是 ERROR

    早先写成 ERROR, 但实测跑下来是 **8 条常驻** —— 3 个真实标准本轮未查证、
    5 个引用本身残缺(``GB/T 17626`` 没有部分号)或复合(``IEC 60898-1/2`` 指两条
    标准)。按本项目 §4.4 已有的裁决: **判 ERROR 会让门禁在种子上永远红, 而永远红
    的门禁会被整体忽略** —— 那比「WARN 但列得清清楚楚」更坏。

    所以降为 WARN, 但**信息量不减**: 每条都带引用者清单, 处置动作写在消息里。
    真要清零, 靠的是把那 8 条逐条查证/修正引用, 不是把门禁调严。

    判 ERROR 的场景仍然存在(引用被改成指向一个**根本不存在**的标准号), 只是
    当前种子还没到那一步 —— 到那时门禁会红, 而那时它本就该红。
    """
    std_nums = {
        str(r.get("id") or "").removeprefix("std::").strip()
        for r in records
        if r.get("entity_type") == "standard"
    }
    unresolved: dict[str, list[str]] = {}
    for rec in records:
        ref = rec.get("authority_ref")
        if not ref:
            continue
        m = STANDARD_ID_HEAD.match(str(ref))
        if m is None:
            continue  # 形态问题已由 authority_shape 报过; 非标准号形态的引用跳过
        head = re.sub(r"\s+", " ", m.group(0)).strip()
        if head not in std_nums:
            unresolved.setdefault(head, []).append(str(rec.get("id") or "<no-id>"))
    for head, users in sorted(unresolved.items()):
        report.add(
            "authority_ref_resolvable",
            "WARN",
            head,
            f"{len(users)} 处引用指向库里不存在的标准实体(示例 {users[:3]})—— "
            f"处置二选一: 补 standard 实体(标准真实存在时), 或改这些 authority_ref"
            f"(引用残缺/复合时, 如 {head!r} 可能是残缺族号或 'A/B' 两标准合写)",
        )


def check_authority_clause_missing(records: list[dict[str, Any]], report: GateReport) -> None:
    """``standard`` 级引用**不带条款号**时 WARN。

    ## 为什么单独一条

    「标准号解析得到」与「这一条引用**核到了条款**」是**两件事**。补了标准实体
    之后, 前者自动成立, 后者不会自动成立 —— 图诊断实测: ``GB/Z 14429-2005``
    一个人被 32 处引用(远动四遥量 ``YC_*``/``YX_*``/``YK_*``), 其中**只有 1 条**
    带条款号(``INSTR_SOE_TESTER`` -> §2.1.45), 其余 31 条只写标准号。

    只写标准号意味着「我引了这条标准」而没有说「引它的哪一条」—— 读者能查到那
    条标准, 但**复核不到这一条知识**。这在红线 5 的语言里是「出处不可查」。

    判 WARN 而不是 ERROR: 引到哪一条需要人工逐条核(有的概念本就是整条标准的
    通则, 没有单一条款), 但**这件事必须被看见** —— 不看见就成了「已核对过」。

    **标准实体本身被排除在外**(实测 30 条, 全部是 ``std::*``)
    ----------------------------------------------------------
    标准实体的 ``authority_ref`` 装的是**查证来源串**而不是条款引用, 实测形态:
    ``全国标准信息公共服务平台 std.samr.gov.cn/``、``GB/T 17626.4-2018``、
    ``JEDEC 官方文档页 jedec.org/...``。也就是说它的「出处」就是它自己。
    而一条标准文档**不存在「自己内部的条款号」** —— 要求 ``std::GB/T 17626.4-2018``
    填 ``clause``, 在语义上就是要求「GB/T 17626.4 的第几章」, 这个问题没有答案。

    早先没排除时这 30 条混在真缺口里, 有两个坏处: 一是把**门禁自身无法满足的项**
    当成待办, 逼人去补一个不存在的字段; 二是让按类型分的缺口统计失真(标准实体
    占了 30/227)。这条判据要的是「引了这条标准但没说是哪一条」, 而标准实体
    根本没引别的东西。
    """
    by_type: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    by_std: Counter[str] = Counter()
    for rec in records:
        if _authority_kind(rec) != "standard":
            continue
        if str(rec.get("entity_type") or "") == "standard":
            continue  # 标准实体的出处是它自己, 没有「内部条款」可言
        ref = str(rec.get("authority_ref") or "")
        if not ref or STANDARD_ID_HEAD.match(ref) is None:
            continue
        if rec.get("clause") or "§" in ref:
            continue
        etype = str(rec.get("entity_type") or "?")
        by_type[etype] = by_type.get(etype, 0) + 1
        samples.setdefault(etype, []).append(str(rec.get("id") or "<no-id>"))
        head = STANDARD_ID_HEAD.match(ref)
        by_std[head.group(0) if head else "(形态不识别的引用)"] += 1
    for etype, n in sorted(by_type.items()):
        # **按标准分组**而不是只给总数: 一个「169」不驱动任何补齐工作 ——
        # 它既没说哪几条标准最值得先补, 也没说补一条要付多少代价。按标准分组后
        # 「GB/T 3187-1994 占 33 条、GB/T 27418-2017 占 22 条」就自带优先级。
        top = "、".join("%s %d 条" % (s, c) for s, c in by_std.most_common(6))
        report.add(
            "authority_clause_missing",
            "WARN",
            etype,
            f"{n} 条 standard 级引用不带条款号(示例 {samples[etype][:3]})—— "
            f"「引了这条标准」不等于「核到了这一条」, 复核时会卡在这里。"
            f"按标准分: {top}" + ("…" if len(by_std) > 6 else ""),
        )


def check_reportables(records: list[dict[str, Any]], ids: set[str], report: GateReport) -> None:
    """WARN/INFO 级: 无出处、无 confidence、孤儿记录。

    这三项都不是错 —— 无出处是知识层的合法状态(只降可信度), 孤儿
    记录依赖「引用字段列全」这个前提。列成规模数字而不是逐条刷屏,
    是因为逐条列 500 行没人看, 而「518 条无出处」能直接驱动补齐工作。
    """
    no_auth: dict[str, int] = Counter()
    for rec in records:
        if _is_relation(rec):
            continue
        if not rec.get("authority_ref") and not rec.get("source"):
            no_auth[str(rec.get("entity_type"))] += 1
    for etype, n in sorted(no_auth.items(), key=lambda kv: -kv[1]):
        report.add("authority_present", "WARN", etype, f"{n} 条无 authority_ref 也无 source")
    no_conf = [r for r in records if not _is_relation(r) and r.get("confidence") is None]
    # 分两层报, 因为两层的**下一步动作不同**: 声明了 authority_kind 的,
    # 可信度已经能从 ``CREDIBILITY_BY_AUTHORITY`` 推出, 只差这次断言本身
    # 有没有查过; 连权威类型都没有的, 是连「该拿什么标准去查」都还不知道。
    # 只报一个总数的话, 588 这个数字驱动不了任何补齐工作。
    graded = sum(1 for r in no_conf if _authority_kind(r))
    report.add(
        "confidence_present",
        "WARN",
        "-",
        f"{len(no_conf)} 条未标 confidence: 其中 {graded} 条已声明 authority_kind "
        f"(可信度可由 CREDIBILITY_BY_AUTHORITY 推出), {len(no_conf) - graded} 条连权威类型都没有",
    )
    by_type: dict[str, int] = Counter(str(r.get("entity_type")) for r in no_conf)
    for etype, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        report.add("confidence_present_by_type", "WARN", etype, f"{n} 条未标 confidence")

    mentioned: set[str] = set()
    for rec in records:
        for fld in REF_FIELDS:
            val = rec.get(fld)
            if not val:
                continue
            mentioned.update(str(v) for v in (val if isinstance(val, list) else [val]))
    orphans = [
        str(r.get("id"))
        for r in records
        if not _is_relation(r) and r.get("id") and str(r["id"]) not in mentioned
    ]
    report.add(
        "orphan_records",
        "INFO",
        "-",
        f"{len(orphans)} 条未被任何引用指向(其中 {sum(1 for o in orphans if str(o).startswith('F_'))} 条公式)",
    )


def check_l0_policy_tables(report: GateReport, dsn: str) -> None:
    """``l0_term`` 表的数据政策核对: 标了「不灌」却有数据 = ERROR, 没标 = WARN。

    查的是「表是不是空的」, 报的却是**审计风险**: 方案 §八 F 实测 13 张表里
    11 张 0 行 —— 22 个 CHECK / 38 个索引 / 1 个触发器全在空转, 而对应 ADR
    全部 ``Accepted``。审计读索引会把「已决策」读成「已实现」(红线 14)。

    **空表本身不是错**(红线 4: 领域知识的权威是 git 内种子 JSON, 进 PG 就是
    双源), 所以空表只进 stats 不报; 要往这些表灌数据得先推翻红线 4, 不是
    绕过一条 WARN。

    政策以 ``COMMENT ON TABLE`` (迁移 ``0004_l0_data_policy``) 为准, 不在本
    文件里复述一份 —— 复述就会出现两个真相源, 而注释是跟着表走的那个。
    """
    import psycopg  # 局部 import: 离线跑门禁不需要驱动

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, obj_description(c.oid)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'l0_term' AND c.relkind = 'r'
            ORDER BY c.relname
            """
        )
        rows = cur.fetchall()
    if not rows:
        report.add(
            "l0_term_missing",
            "WARN",
            "l0_term",
            "l0_term schema 不存在 (迁移未跑?) —— 无法核对数据政策",
        )
        return
    empty = 0
    for name, comment in rows:
        policy = (comment or "").strip()
        if not policy:
            report.add(
                "l0_policy_unlabeled",
                "WARN",
                f"l0_term.{name}",
                "表没有 COMMENT 数据政策; 审计无法区分「按红线 4 不灌」与「忘了灌」",
            )
            continue
        if not policy.startswith("按政策不灌"):
            continue
        empty += 1
        n = _live_count(dsn, name)
        if n:
            report.add(
                "l0_policy_violated",
                "ERROR",
                f"l0_term.{name}",
                f"表上写着「按政策不灌」(红线 4), 实际有 {n} 行 —— "
                "要么数据是漂移的副本, 要么政策注释过期了; 二选一, 别放着",
            )
    report.stats["l0_tables"] = len(rows)
    report.stats["l0_policy_empty"] = empty


def _live_count(dsn: str, table: str) -> int:
    """``count(*)``: 空与非空的判据不能是 reltuples 的估算值。

    表名来自 ``pg_class`` 而非用户输入, 但仍用标识符引号包起来 ——
    拼接 SQL 只在名字不可信时才需要引号, 这里只需要一条注释说明来源。
    """
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM l0_term."{table}"')  # noqa: S608
        row = cur.fetchone()
    return int(row[0]) if row else 0


# ============================================================================
# 知识覆盖度 (2026-10-08 新增)
# ============================================================================
#
# 为什么要有这条
# --------------
# 2026-10 那一轮补数据把种子的权威分层从「unverified 431 / standard 127 /
# industry 16」改成「unverified 28 / standard 289 / industry 264」, 孤立率从 74.0%
# 降到 48.1%(越过 SPARSE_GRAPH_RATIO, sparseness_warning 于是消失)。
#
# **那次改善当时没有任何东西盯着** —— 门禁没有这一项, 于是:
#   - 改善本身不可见(没人知道跨过了阈值);
#   - **回退也不可见**(把实体倒回 unverified、把 confidence 删掉, 门禁照样全绿)。
# 这正是本文件开头那条纪律说的「永远不红的门禁会被整体忽略」的另一面:
# 只查「坏」不查「退化」, 那么好状态和坏状态长得一样。
#
# 所以这里查的是**比例**而不是绝对数 —— 绝对数会随种子增长漂移, 比例不会。
# 阈值取得比现状**宽**一截(现状 unverified 4.7%, 阈值 15%), 这样它拦的是
# 「大批东西被倒回未查证」而不是「多了一条新知识就红」。

#: ``unverified`` 实体占比上限。现状 28/592 = 4.7%。定 15% 是为了给「新增但
#: 还没查证的知识」留出空间 —— 未查证本身不是错误(可能确实查不到标准条款),
#: 但**占比**高到一定程度就说明查证工作停了。
UNVERIFIED_RATIO_MAX = 0.15

#: 带 ``confidence`` 的实体占比下限。现状 564/593 = 95.3% —— 定 90% 而不是 95%,
#: 因为那 0.28pp 的余量太薄: 再多一条实体没 confidence 就会翻红, 于是这条检查会被
#: 当成噪声忽略, 那比没有更糟。定 90% 拦的是「成批回退」, 不是「多一条」。
#:
#: 残留的 28 条无 confidence 是**合理**的: 21 个本项目自定义记号(``d_env``/``k_sim``/
#: ``CTI``/``TUR``… 挂标准号是假的) + 7 条 erratum(我们自己对方案原文的修正记录)。
#: 它们不是「忘了填」, 是「按设计就没有标准可引」。
CONFIDENCE_COVER_MIN = 0.90


def check_authority_coverage(records: list[dict[str, Any]], report: GateReport) -> None:
    """权威分层与 confidence 覆盖率 (比例判据, 不是绝对数)。

    **只报 stats 不报 finding 的情况是有意的**: 达标时不制造噪声, 让「退化」这条
    finding 真正醒目。判 WARN 不判 ERROR —— 与 :func:`check_graph_structure` 同一
    纪律: 覆盖率不足是「工作没做完」, 不是「数据坏了」, 判 ERROR 会让门禁习惯性红。
    """
    ents = [r for r in records if not _is_relation(r)]
    rels_recs = [r for r in records if _is_relation(r)]
    total = len(ents)
    if not total:
        report.add("authority_coverage", "ERROR", "<no-entity>", "记录里没有实体")
        return

    ak = Counter(str(_authority_kind(r)) for r in ents)
    with_conf = sum(1 for r in ents if r.get("confidence") is not None)
    unv = ak.get("unverified", 0)
    unv_ratio = unv / total
    conf_ratio = with_conf / total

    # 关系记录里的自环: 不含信息, 且 materialize 建图时会丢弃 —— 于是「记录数」
    # 与「实际建边数」永远对不上, 按记录数审图的人会把它当成真边。必须可见。
    self_loops = sum(1 for r in rels_recs
                     if str(r.get("source_id")) == str(r.get("target_id")))

    report.stats.update(
        {
            "authority_by_kind": dict(ak.most_common()),
            "unverified_ratio": round(unv_ratio, 4),
            "confidence_cover": round(conf_ratio, 4),
            "relation_self_loops": self_loops,
        }
    )

    if unv_ratio > UNVERIFIED_RATIO_MAX:
        report.add(
            "authority_coverage", "WARN", "<seed>",
            "unverified 占比 %.1f%% 超过上限 %.0f%% (%d/%d) —— 查证工作可能停了"
            % (unv_ratio * 100, UNVERIFIED_RATIO_MAX * 100, unv, total),
        )
    if conf_ratio < CONFIDENCE_COVER_MIN:
        report.add(
            "authority_coverage", "WARN", "<seed>",
            "confidence 覆盖率 %.1f%% 低于下限 %.0f%% —— 缺失的语义是「未查证」"
            % (conf_ratio * 100, CONFIDENCE_COVER_MIN * 100),
        )
    if self_loops:
        report.add(
            "authority_coverage", "WARN", "<seed>",
            "关系记录里有 %d 条自环(source==target) —— 建图时会被丢弃, "
            "使「关系记录数」与「实际建边数」对不上" % self_loops,
        )


def run_gate(records: list[dict[str, Any]]) -> GateReport:
    """跑全部门禁检查, 返回报告。**不修改任何数据**。"""
    report = GateReport()
    ids = check_ids(records, report)
    check_refs(records, ids, report)
    check_undeclared_id_tokens(records, ids, report)
    check_authority_shape(records, report)
    check_authority_refsolvable(records, report)
    check_authority_clause_missing(records, report)
    check_reportables(records, ids, report)
    check_graph_structure(records, report)
    check_authority_coverage(records, report)
    by_type = Counter(str(r.get("entity_type")) for r in records)
    # ``update`` 而不是赋值: :func:`check_ids` 已经往 stats 里放了
    # ``bare_name_ids``, 赋值会把它抹掉 —— 而那条正是「已登记的裸名 id
    # 有多少」的漂移指标。
    report.stats.update(
        {
            "records": len(records),
            "with_id": len(ids),
            "by_type": dict(by_type),
            "relations": sum(1 for r in records if _is_relation(r)),
        }
    )
    return report


def check_graph_structure(records: list[dict[str, Any]], report: GateReport) -> None:
    """图结构健康 (方案 §4.4 的 ``GraphValidator`` 产出)。

    **判据为什么是 WARN 而不是 ERROR**: 孤立节点与薄覆盖都不是「数据坏了」, 而是
    「关系还没建够」—— 合法但值得知道的状态。把它判 ERROR 会让门禁在种子上永远红,
    而永远红的门禁会被整体忽略, 于是真正该看的 ERROR 也一起没人看了。

    但有一类是真的坏: **图建不起来** (``validate_structure`` 抛异常)。那说明适配层
    或数据形状坏了, 静默跳过等于把「查不了」报成「没问题」, 所以那条判 ERROR。
    """
    from aterag.kg import analytics

    try:
        graph = analytics.graph_from_records(records)
        v = analytics.validate_structure(graph)
    except Exception as e:  # noqa: BLE001 图建不起来是真问题, 不是「没查」
        report.add(
            "graph_structure",
            "ERROR",
            "graph",
            f"图结构检查未完成: {type(e).__name__}: {e}",
        )
        return

    topo = v["topology"]
    report.stats["graph_nodes"] = topo["nodes"]
    report.stats["graph_edges"] = topo["edges"]
    report.stats["graph_isolated"] = topo["isolated_nodes"]
    report.stats["graph_largest_component"] = topo["largest_component"]

    # 只有这一条判 ERROR, 且**要求多节点**。单节点样本(测试夹具、单条记录重跑)
    # 本来就一条边都没有, 拿它判 ERROR 会让门禁在最小输入上永远红 —— 而永远红
    # 的门禁会被整体忽略。早先另写了个 `elif edges == 0` 分支, 但 edges 为 0 必然
    # 意味着所有节点孤立, 那个分支永远不可达 —— 不可达的判据就是装饰, 而且它与本条
    # 的文案撞车, 让人以为两条规则都在生效。
    if topo["nodes"] > 1 and topo["isolated_nodes"] == topo["nodes"]:
        report.add(
            "graph_structure",
            "ERROR",
            "graph",
            f"图里 {topo['nodes']} 个节点全部孤立 —— 一条边都没有, 图等于不存在"
            "(实体都在但关系全丢了, 通常是关系记录的目标 id 全部解析不到)",
        )

    ratio = topo["isolated_nodes"] / topo["nodes"] if topo["nodes"] else 0.0
    if topo["edges"] and ratio > 0.5:
        report.add(
            "graph_structure",
            "WARN",
            "graph",
            f"{topo['isolated_nodes']}/{topo['nodes']} 个节点孤立 ({ratio:.0%}); "
            f"最大连通分量仅 {topo['largest_component']} —— 图分析类产出(中心性/追溯)"
            f"在这种稀疏度下信息量有限, 要让它们有意义得先补关系",
        )

    for issue in v["issues"]:
        sev = str(issue.get("severity", "")).upper()
        if "ERROR" in sev:
            report.add("graph_structure", "ERROR", issue.get("node") or "graph", issue["message"])
        elif "WARNING" in sev and "orphan" not in issue["message"].lower():
            report.add("graph_structure", "WARN", "graph", issue["message"])

    thin = analytics.thin_coverage(graph)
    if thin["total"]:
        report.stats["graph_thin_covered"] = thin["count"]
        if thin["count"]:
            report.add(
                "graph_thin_covered",
                "WARN",
                thin["node_type"],
                f"{thin['count']}/{thin['total']} 个 {thin['node_type']} 节点入度 <= {thin['threshold']}"
                f" (其中 {thin['zero_in_degree']} 个为 0) —— 覆盖薄弱, 相关分析用不上它们",
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="知识门: 种子记录集的质量门禁")
    ap.add_argument(
        "--seed",
        default="data/seed/power_domain_seed.json",
        help="种子 JSON 路径(默认 data/seed/power_domain_seed.json)",
    )
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    ap.add_argument(
        "--pg-dsn",
        help=(
            "PG DSN。给了才核对 l0_term 的数据政策(表上写着「按政策不灌」却有数据 "
            "= ERROR, 没写政策 = WARN); 不给则显式记 pg_policy_check=skipped —— "
            "静默跳过会把「没连上」和「查过了没问题」混成一条"
        ),
    )
    args = ap.parse_args(argv)

    seed_path = Path(args.seed)
    if not seed_path.is_file():
        print(f"种子文件不存在: {seed_path}", file=sys.stderr)
        return 2
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    report = run_gate(list(seed.get("records") or []))

    if args.pg_dsn:
        try:
            check_l0_policy_tables(report, args.pg_dsn)
            report.stats["pg_policy_check"] = "checked"
        except Exception as e:  # noqa: BLE001 连不上也要说出来, 不能当成通过
            report.stats["pg_policy_check"] = f"failed: {type(e).__name__}: {e}"
            report.add(
                "pg_policy_unavailable",
                "WARN",
                "l0_term",
                f"数据政策核对未完成: {type(e).__name__}: {e}",
            )
    else:
        report.stats["pg_policy_check"] = "skipped (未给 --pg-dsn)"

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        st = report.stats
        print(f"知识门 {seed_path}: {st['records']} 条记录 ({st['with_id']} 带 id, {st['relations']} 关系)")
        if "l0_tables" in st:
            print(
                f"  l0_term 数据政策: {st['l0_tables']} 张表, "
                f"其中 {st['l0_policy_empty']} 张按红线 4 不灌"
            )
        print(f"  l0_term 政策核对: {st['pg_policy_check']}")
        if "graph_nodes" in st:
            print(
                f"  图结构: {st['graph_nodes']} 节点 / {st['graph_edges']} 边 "
                f"(孤立 {st['graph_isolated']}, 最大连通分量 {st['graph_largest_component']})"
                + (
                    f"; 覆盖薄弱 {st['graph_thin_covered']} 个"
                    if "graph_thin_covered" in st
                    else ""
                )
            )
        for f in report.findings:
            if f.severity == "INFO":
                continue
            print(f"  [{f.severity}] {f.check}: {f.where} — {f.detail}")
        print(
            f"  汇总: ERROR {report.count('ERROR')} / WARN {report.count('WARN')} / "
            f"INFO {report.count('INFO')}"
        )
        if report.count("ERROR") == 0:
            print("  门禁通过")
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
