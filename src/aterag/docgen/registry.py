"""公式总表生成(§18.6 步骤 1~7) -> ``seed/formula.csv``。

§18.10 注 9: ``seed/*.csv`` 必须由 ``docgen`` 生成, **禁止手工维护** ——
手工维护的 CSV 与文档必然漂移。本模块是唯一写 ``seed/formula.csv`` 的地方。

## 本模块最重要的行为:拒绝编造

``formula`` 表有两列 ``NOT NULL`` 在方案的公式表里**根本没有**:

==============  ==========================  ==========================
列缺��          方案公式表里的实际情况      实测缺口(129 条闭合公式)
==============  ==========================  ==========================
``name_zh``     只对部分公式给了中文名      **83 / 129** 缺
``source_ref``  **该列不存在**              **129 / 129** 缺
==============  ==========================  ==========================

所以严格按 ``NOT NULL`` 过滤, **可入库条数是 0**。这不是 bug, 是方案侧的
两处缺口(与 ``U``/``u_c`` 的量纲矛盾并列为第三处)。

§18.10 注 8 明确写着「**标准条款号不可编造**」, 所以本模块**拒绝**给
``source_ref`` 填占位值或猜出来的标准号 —— 那正是本项目一路在防的
「看起来合理但错误」。缺口由 :class:`RejectionReport` **机械上报**,
而不是靠散文描述。

## 数组与向量的编码

CSV 没有数组类型, 故:

- ``TEXT[]`` -> ``|`` 分隔(``A-2|A-4|T3``)。方案自身的写法也是 ``|``。
- ``NUMERIC(8,4)[]`` -> **7 个数、空格分隔**, 顺序固定为
  长度/质量/时间/电流/温度/物质的量/发光强度(``solver.symbolic.Dimension``
  的字段顺序)。顺序写死在 :data:`DIMENSION_ORDER`, 不靠调用方记忆。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .derive import Derivation, derive_all
from .errata import parse_errata, reverse_index
from .expr_norm import normalize_equation
from .quantity_rules import build_dictionary
from .render import Rendered, render_equation
from .spec_parse import FormulaRow, parse_formula_rows, read_spec
from .standard_terms import Term, resolve
from .standards import parse_standards
from .symbols import SymbolTable, parse_symbol_table

__all__ = [
    "DIMENSION_ORDER",
    "FormulaRecord",
    "RejectionReport",
    "build_records",
    "write_formula_csv",
]

SPEC_PATH = (
    Path(__file__).resolve().parents[3]
    / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md"
)

#: ``dimension_vec`` 的分量顺序。**固定**, 与 ``solver.symbolic.Dimension``
#: 的字段顺序一致; CHECK 只验 ``cardinality = 7``, 值序错了不会被数据库拦下。
DIMENSION_ORDER: tuple[str, ...] = (
    "length", "mass", "time", "current", "temperature", "amount", "luminous_intensity",
)

_ARRAY_SEP = "|"

#: §18.3.1 允许的 ``scope`` 取值。
SCOPES = ("global", "shared_l0", "model")

#: 域字母 -> ``domain_tags``。方案给的是单字母, 标签用它可读形式。
_DOMAIN_TAG = {
    "E": "电气", "J": "变换器拓扑", "K": "控制与环路", "L": "保护",
    "M": "监控与遥测", "N": "可靠性", "O": "安规", "P": "电磁兼容",
    "Q": "热设计", "R": "并网与互连", "S": "结构", "T": "测试",
    "W": "整机与工艺", "H": "谐波", "G": "通用",
}

_DOMAIN_RE = re.compile(r"F_([A-Z])(?=[._])")

#: **隔离名单**: 已确认公式与其自身声明量纲不符(方案侧错误), 或成因未定。
#:
#: 这些公式**不入库**, 但也不从报告里消失 —— 它们列在
#: :class:`RejectionReport` 的 ``quarantined`` 里, 附原因与出处。
#:
#: 隔离而不是删��: 删掉就看不出「方案里有过这么一条且它有问题」, 而
#: §18.10 注 8 禁止我方编造一个「看起来对」的式子替它。逐条依据见
#: ``docs/w1-triage.md``。
QUARANTINED: dict[str, str] = {
    # —— 方案错误:公式与其自带量纲列冲突, 量纲算术即可定案 ——
    "F_J.8.3_VDS_SPIKE": "方案错误: A·H·Hz 精确等于 V, 故 /C_oss 多余; "
    "方案自记的 [A]·[H]·[Hz]/[F]=[V] 算术上即错",
    "F_S.5_EFFICIENCY_CURVE": "方案错误: sqrt(W·Ω) 精确等于 V, 与声明的 [W] 冲突",
    "F_J.8.1_SHOOT_THROUGH": "方案错误: 右侧 A/s 而非 A, 疑缺死区时间因子 t_dead",
    "F_J.6.2_DCM_PEAK_CURRENT": "方案错误(待作者确认): 疑多余因子 D²·V_in; "
    "sqrt(2P/(L·f)) 量纲自洽",
    # —— 成因未定: 不敢归给方案, 也不敢归给自己 ——
    "F_L.5.6_PFH_1001D": "成因未定: 右侧 λ_DU·T_proof 无量纲, 而 PFH 常被引为 1/h; "
    "IEC 61508 对 1001D 引用纯数。约定需作者确认",
}


@dataclass
class HomogeneityReport:
    """量纲**齐次性**检查结果 —— 这才是 §18.9 G1 要的东西。

    「符号都能查到量纲」只说明**符号有定义**, 不说明**方程自洽**。
    §18.10 注 3 明写「量纲校验是 R1 的机器实现, 必须在入库阶段拦截」。
    实测 130 条候选里只有 113 条齐次, 所以这道检查放在 registry 里(入库前),
    而不只是门禁里(入库后)。
    """

    homogeneous: list[str] = field(default_factory=list)
    #: (公式 ID, 归一化右侧, 原因)
    inhomogeneous: list[tuple[str, str, str]] = field(default_factory=list)
    #: (公式 ID, 引擎不支持的原因)
    unsupported: list[tuple[str, str]] = field(default_factory=list)


def check_homogeneity(
    first: dict[str, FormulaRow],
    closed: dict[str, tuple[str, ...]],
    lhs_text: dict[str, str],
    rhs_text: dict[str, str],
    symbols: SymbolTable,
) -> HomogeneityReport:
    """对每条符号全可解析的公式跑 :func:`solver.symbolic.check_expression`。

    LHS 量纲取左侧的**静态乘积**(纯乘除幂)。带加减或函数调用的左侧静态求不出,
    记入 ``unsupported`` —— **不猜**。
    """
    from ..solver.symbolic import (
        Dimension,
        ExpressionError,
        VariableSpec,
        check_expression,
    )

    report = HomogeneityReport()
    dictionary = build_dictionary(symbols)
    for fid, variables in closed.items():
        specs = {
            v: VariableSpec(name=v, dimension=d)
            for v, d in (
                (v, dictionary.resolve(v, _domain_letter(fid), fid).dimension) for v in variables
            )
            if d is not None
        }
        vec = _lhs_dimension(lhs_text.get(fid), dictionary, _domain_letter(fid))
        lhs = Dimension(**dict(zip(DIMENSION_ORDER, vec))) if vec else None
        rhs = rhs_text.get(fid)
        if rhs is None:
            report.unsupported.append((fid, "无归一化右侧可校验"))
            continue
        try:
            result = check_expression(rhs, specs, formula_id=fid, lhs_dimension=lhs)
        except ExpressionError as exc:
            report.unsupported.append((fid, str(exc)[:90]))
            continue
        if result.dimension_ok:
            report.homogeneous.append(fid)
        else:
            report.inhomogeneous.append((fid, rhs[:60], result.reason or "两侧量纲不等"))
    return report


#: ``formula_id`` 的英文短名部分: ``F_J.5.2_RIPPLE_RMS_SQ`` -> ``RIPPLE_RMS_SQ``。
#:
#: 方案用「域字母.章.节_英文短名」的形式给公式 ID, **短名是方案自己定义的**,
#: 所以拿它当名称不是编造 —— 这是与「用 websearch 猜一个中文译名」的本质区别:
#: 前者的每个字都能在方案里指到出处。
_ID_SHORT_NAME_RE = re.compile(r"^F_[A-Z](?:\.\d+)*_(.+)$")

#: ``name_zh`` 的来源, 必须可区分。
NAME_SOURCE_DECLARED = "declared"        # 方案自写中文名
NAME_SOURCE_STANDARD = "standard"        # 标准术语(GB 国标优先), 后缀带标准号
NAME_SOURCE_ID = "id-short-name"         # 方案只给英文短名, 照用


def _standard_term_for(formula_id: str, short_name: str | None) -> Term | None:
    """用公式短名去标准术语表查中文名; 查不到返回 ``None``。

    先按**完整短名**查(``MTBF`` 这种纯缩写最可靠), 再按短名里的首个大写词
    查(``MTBF_GAUGE`` -> ``MTBF``)。都不中就放弃 —— **不猜、不截断匹配**。
    """
    if not short_name:
        return None
    hit = resolve(short_name)
    if hit is not None:
        return hit
    head = short_name.split("_")[0]
    return resolve(head)


def _english_short_name(formula_id: str) -> str | None:
    """取公式 ID 的英文短名; 取不到就返回 ``None``(不拿 ID 整串糊弄)。"""
    match = _ID_SHORT_NAME_RE.match(formula_id)
    return match.group(1) if match else None


def _meanings_of(symbols: SymbolTable) -> dict[str, str]:
    """U.5 的「含义」列 -> ``{符号: 中文}``, 供 ``expr_plaintext`` 渲染。

    §18.3.1 要求 ``expr_plaintext`` 是「非 LaTeX 自然语言表述」。不传这张表
    的话渲染器会**原样保留符号**(见 ``render`` 模块「未翻译的符号不许静默」),
    产出 ``V_out等于D乘以V_in`` 这种半成品 —— 合法但没起到该起的作用。

    一个符号有多条 U.5 条目时取**第一条非空**含义: 含义是给人看的, 选哪个都
    不影响量纲, 不必为它引入歧义判定。
    """
    out: dict[str, str] = {}
    for entry in symbols.entries:
        meaning = (entry.meaning or "").strip()
        if meaning and entry.symbol not in out:
            out[entry.symbol] = meaning
    return out


def _domain_letter(formula_id: str) -> str | None:
    match = _DOMAIN_RE.match(formula_id)
    return match.group(1) if match else None


@dataclass(frozen=True)
class FormulaRecord:
    """一条可入库的公式(所有 ``NOT NULL`` 列均已满足)。"""

    formula_id: str
    name_zh: str
    #: ``name_zh`` 的来源: ``declared``(方案给了中文名)/ ``id-short-name``
    #: (方案只给英文短名, 照用)。**不做美化** —— ``PFH_1001D`` 转成
    #: 「Pfh 1001d」反而更难认。
    name_source: str
    domain: str
    section: str
    domain_tags: tuple[str, ...]
    var_refs: tuple[str, ...]
    dimension_vec: tuple[float, ...]
    derive_from: tuple[str, ...]
    source_ref: str
    #: ``source_ref`` 的来源类别: ``standard``(附录 V 反查到的标准号)/
    #: ``section``(方案章节号)。**不是标准号**的必须标明, 否则下游会把它当
    #: 认证依据用 —— 那是 §18.10 注 8 明令禁止的。
    source_kind: str
    rendered: Rendered
    errata: str | None = None
    boundary: str | None = None
    name_en: str | None = None
    used_by_rule: tuple[str, ...] = ()
    used_by_test: tuple[str, ...] = ()
    used_by_axon: tuple[str, ...] = ()
    confidence: float = 0.95
    #: 方案自己写的中文名(若与 ``name_zh`` 不同则保留), 供追溯漂移。
    #: 放在末尾是因为 dataclass 要求无默认值字段在前。
    name_zh_declared: str | None = None

    def as_csv_row(self) -> dict[str, str]:
        """转成与 ``formula`` 表同名的 CSV 行(列序见 :data:`CSV_COLUMNS`)。"""
        row: dict[str, str] = {
            "formula_id": self.formula_id,
            "name_zh": self.name_zh,
            "name_zh_declared": self.name_zh_declared or "",
            "name_source": self.name_source,
            "name_en": self.name_en or "",
            "domain": self.domain,
            "section": self.section,
            "domain_tags": _ARRAY_SEP.join(self.domain_tags),
            "var_refs": _ARRAY_SEP.join(self.var_refs),
            "dimension_vec": " ".join(f"{v:.4f}" for v in self.dimension_vec),
            "dimension_ok": "true",
            "derive_from": _ARRAY_SEP.join(self.derive_from),
            "boundary": self.boundary or "",
            "confidence": f"{self.confidence:.2f}",
            "scope": "global",
            "used_by_rule": _ARRAY_SEP.join(self.used_by_rule),
            "used_by_test": _ARRAY_SEP.join(self.used_by_test),
            "used_by_axon": _ARRAY_SEP.join(self.used_by_axon),
            "errata": self.errata or "",
            "source_ref": self.source_ref,
            "source_kind": self.source_kind,
        }
        row.update({k: v or "" for k, v in self.rendered.as_row().items()})
        return row


#: CSV 列序。与 §18.3.1 的列序一致, 便于人工比对。
CSV_COLUMNS: tuple[str, ...] = (
    "formula_id", "name_zh", "name_zh_declared", "name_source", "name_en",
    "domain", "section", "domain_tags",
    "var_refs", "dimension_vec", "dimension_ok", "derive_from", "boundary",
    "confidence", "scope", "used_by_rule", "used_by_test", "used_by_axon",
    "errata", "source_ref", "source_kind",
    "expr_latex", "expr_plaintext", "expr_ascii", "expr_ast",
)


@dataclass
class RejectionReport:
    """入库过滤结果。**缺口必须机械上报, 不能靠散文描述。**"""

    #: 拒收原因 -> 公式 ID
    reasons: dict[str, list[str]] = field(default_factory=dict)
    #: 因缺 ``name_zh`` 被拒
    missing_name_zh: list[str] = field(default_factory=list)
    #: 因缺 ``source_ref`` 被拒(方案公式表**无此列**)
    missing_source_ref: list[str] = field(default_factory=list)
    #: 量纲闭合但算不出 ``dimension_vec``
    missing_dimension: list[str] = field(default_factory=list)
    #: 四种表达渲染失败
    render_failed: list[str] = field(default_factory=list)
    #: (公式 ID, 方案原名, 标准名, 标准号) —— 两者不一致, 记下来供人看
    name_conflicts: list[tuple[str, str, str, str]] = field(default_factory=list)
    #: 被隔离的公式 -> 原因(方案错误或成因未定)
    quarantined: dict[str, str] = field(default_factory=dict)
    considered: int = 0

    def reject(self, reason: str, fid: str) -> None:
        self.reasons.setdefault(reason, []).append(fid)

    def summary(self) -> str:
        parts = [f"考虑 {self.considered} 条量纲闭合公式", f"隔离 {len(self.quarantined)} 条"]
        for reason in sorted(self.reasons):
            parts.append(f"{reason}: {len(self.reasons[reason])}")
        return "; ".join(parts)


def _domain_of(formula_id: str, row: FormulaRow) -> str | None:
    if row.domain:
        return row.domain
    match = _DOMAIN_RE.match(formula_id)
    return match.group(1) if match else None


def _dimension_vec(dims: list[float] | None) -> tuple[float, ...] | None:
    if dims is None or len(dims) != len(DIMENSION_ORDER):
        return None
    return tuple(dims)


def build_records(
    lines: list[str],
) -> tuple[tuple[FormulaRecord, ...], RejectionReport]:
    """从方案抽出全部**可入库**的公式, 并如实上报被拒原因。

    只收量纲闭合的公式 —— ``dimension_ok`` 是合并阻断门禁 G1 的判据。
    """
    rows = parse_formula_rows(lines).rows
    first: dict[str, FormulaRow] = {}
    for row in rows:
        first.setdefault(row.formula_id, row)

    dictionary = build_dictionary(parse_symbol_table(lines)[0])
    derivations = derive_all(first)
    errata_report = parse_errata(lines)
    # 勘误 -> 公式: 由 Erratum.formula_refs(段前缀)解析, 与门禁 G5 同一套逻辑。
    errata_of: dict[str, str] = {}
    for item in errata_report.errata:
        for fid in _resolve_prefixes(tuple(item.formula_refs or ()), first):
            errata_of.setdefault(fid, str(item.tag))
    # 公理 -> 公式(by_rule / by_test), 用于填 used_by_* 列。
    used_rule: dict[str, list[str]] = {}
    used_test: dict[str, list[str]] = {}
    for aid, targets in reverse_index(errata_report).get("by_rule", {}).items():
        for ref in targets:
            for fid in _resolve_prefixes((ref,), first):
                used_rule.setdefault(fid, []).append(str(aid))
    for gid, targets in reverse_index(errata_report).get("by_test", {}).items():
        for ref in targets:
            for fid in _resolve_prefixes((ref,), first):
                used_test.setdefault(fid, []).append(str(gid))

    # 标准 -> 公式(反向): 公式表没有 source_ref 列, 只能从这里反查。
    standards_of: dict[str, list[str]] = {}
    for record in parse_standards(lines).records:
        for fid in _resolve_prefixes(tuple(record.formula_refs or ()), first):
            standards_of.setdefault(fid, []).append(str(record.standard_id))

    report = RejectionReport()
    out: list[FormulaRecord] = []

    # 齐次性检查: **入库前**就跑, 不依赖门禁那一侧。G1 是入库后的 CI 门禁,
    # 若只在那里拦, 就会有一批「已写进 CSV」的公式其实没过齐次性。
    lhs_text: dict[str, str] = {}
    rhs_text: dict[str, str] = {}
    closed_vars: dict[str, tuple[str, ...]] = {}
    for fid, row in first.items():
        n = normalize_equation(row.expression)
        if not n.ok:
            continue
        ns = _domain_letter(fid)
        if any(dictionary.resolve(v, ns, fid).dimension is None for v in n.variables):
            continue
        closed_vars[fid] = n.variables
        if n.lhs:
            lhs_text[fid] = n.lhs
        if n.rhs:
            rhs_text[fid] = n.rhs
    symbols = parse_symbol_table(lines)[0]
    homo = check_homogeneity(first, closed_vars, lhs_text, rhs_text, symbols)
    not_homogeneous = {fid: reason for fid, _e, reason in homo.inhomogeneous}
    engine_gap = dict(homo.unsupported)

    for fid, row in first.items():
        normalized = normalize_equation(row.expression)
        if not normalized.ok:
            continue
        ns = _domain_letter(fid)
        if fid not in closed_vars:
            continue  # 未闭合, 不是候选
        report.considered += 1

        # 不齐次 / 引擎判不了 -> 隔离。**绝不入库**: 入库即宣称已验证。
        if fid in not_homogeneous:
            report.quarantined[fid] = f"量纲不齐次: {not_homogeneous[fid]}"
            continue
        if fid in engine_gap:
            report.quarantined[fid] = f"引擎判不了: {engine_gap[fid]}"
            continue

        # --- 隔离名单优先于一切 NOT NULL 检查 ---
        # 已确认与自身声明量纲冲突的式子**不入库**, 也不去「修」它 ——
        # §18.10 注 8 禁止编造。依据见 docs/w1-triage.md。
        if fid in QUARANTINED:
            report.quarantined[fid] = QUARANTINED[fid]
            continue

        # --- 无变量的「公式」是指引散文, 不是公式 ---
        # 实测 F_W.6.13 的 ``需 t_resolve ≈ 1 个时钟周期`` 归一化成常数 1。
        if not normalized.variables:
            report.quarantined[fid] = "表达式无任何变量(指引散文, 非公式)"
            continue

        # --- name_zh 的取值: 三级来源, 全部可追溯 ---
        #
        # 优先级: 标准术语(GB 国标优先) > 方案自写中文名 > 公式 ID 英文短名。
        #
        # 为什么标准名排在方案原名**之前**(用户明确要求): 标准术语有出处、可被
        # 引用与核对, 方案自写名只是作者当时的措辞。但**方案原名照样保留在
        # ``name_zh_declared`` 列** —— 两者不一致时, 漂移的可见性比「谁赢」更重要,
        # 且 §18.10 注 9 要求文档与代码同源, 不能让方案原文被静默替换掉。
        short = _english_short_name(fid)
        std = _standard_term_for(fid, short)
        if row.name_zh:
            name_zh, name_source = row.name_zh, NAME_SOURCE_DECLARED
            if std is not None and std.zh != row.name_zh:
                # 方案名与标准名不一致 —— 记下来让人看, 不自动改写方案原文。
                report.name_conflicts.append((fid, row.name_zh, std.zh, std.standard_id))
        elif std is not None:
            name_zh, name_source = std.zh, f"{NAME_SOURCE_STANDARD}:{std.standard_id}"
        elif short:
            name_zh, name_source = short, NAME_SOURCE_ID
        else:
            report.quarantined[fid] = "既无中文名也无英文短名(公式 ID 无短名部分)"
            continue
        if not row.name_zh:
            report.missing_name_zh.append(fid)
        # ``source_ref`` 的来源优先级: 公式表自带 > 附录 V 反查标准号 >
        # **方案章节号**。
        #
        # 最后一档是 D1 决定 (a) 的落地: 公式表**根本没有**这一列(实测全篇
        # 公式表都没有「标准/来源」列角色), 而 §18.10 注 8 明写「标准条款号
        # 不可编造」—— 所以绝不编造标准号, 改记**方案章节出处**, 并在
        # provenance 里标明这不是标准号。
        standard = row.source_ref or next(iter(standards_of.get(fid, ())), None)
        if standard:
            source, source_kind = standard, "standard"
        elif row.section:
            source, source_kind = f"V6.0§{row.section}", "section"
        else:
            source, source_kind = None, "none"
        if not source:
            report.missing_source_ref.append(fid)
            report.reject("缺 source_ref(无标准号、无章节号)", fid)
            continue
        domain = _domain_of(fid, row)
        section = row.section
        if not domain or not section:
            report.reject("缺 domain/section", fid)
            continue

        vec = _dimension_vec(_lhs_dimension(normalized.lhs, dictionary, ns))
        if vec is None:
            report.missing_dimension.append(fid)
            report.reject("算不出 dimension_vec(左侧量纲不齐)", fid)
            continue

        derivation: Derivation | None = derivations.get(fid)
        if derivation is None or not derivation.refs:
            report.reject("缺 derive_from", fid)
            continue

        rendered = render_equation(
            normalized.lhs,
            normalized.rhs or "",
            normalized.relation or "=",
            _meanings_of(symbols),
        )
        if rendered.error:
            report.render_failed.append(fid)
            report.reject(f"渲染失败: {rendered.error}", fid)
            continue

        out.append(
            FormulaRecord(
                formula_id=fid,
                name_zh=name_zh,
                name_source=name_source,
                name_zh_declared=row.name_zh,
                domain=domain,
                section=section,
                domain_tags=(_DOMAIN_TAG.get(domain, domain),),
                var_refs=tuple(normalized.variables),
                dimension_vec=vec,
                derive_from=tuple(derivation.refs),
                source_ref=source,
                source_kind=source_kind,
                rendered=rendered,
                errata=errata_of.get(fid),
                used_by_rule=tuple(sorted(set(used_rule.get(fid, ())))),
                used_by_test=tuple(sorted(set(used_test.get(fid, ())))),
            )
        )

    return tuple(out), report


def _lhs_dimension(
    lhs: str | None,
    dictionary: object,
    ns: str | None,
) -> list[float] | None:
    """算 ``dimension_vec``: 取**左侧**的量纲向量。

    左侧是等号左边那一串。求解需其全部变量量纲可解析且**彼此齐次** ——
    不齐次说明这条式子左侧是个复合量(如 ``a/b``), 与右侧整体对齐才正确,
    故直接返回 None 让调用方拒收, 而不是猜一个。
    """
    if not lhs:
        return None
    resolved = _dimension_of_tree(lhs, dictionary, ns)
    if resolved is None:
        return None
    total = [0.0] * len(DIMENSION_ORDER)
    for vec, _name in resolved:
        for i, value in enumerate(vec):
            total[i] += value
    return total


def _dimension_of_tree(
    expr: str,
    dictionary: object,
    ns: str | None,
) -> list[tuple[list[float], str]] | None:
    """把左侧文本拆成「(量纲向量, 符号名)」的乘积列表。

    刻意只处理**纯乘除**的左侧(``V_out``、``I_rms^2·R`` 这类)。带加减的
    左侧无法按乘积求和, 返回 None 让调用方拒收 —— 而不是取首项。
    """
    import ast as _ast

    try:
        tree = _ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    out: list[tuple[list[float], str]] = []
    if not _walk_product(tree.body, dictionary, ns, out):
        return None
    return out


def _walk_product(
    node: object,
    dictionary: object,
    ns: str | None,
    out: list[tuple[list[float], str]],
) -> bool:
    """把**纯乘除幂**的表达式拆成量纲贡献的加减。

    加减出现在左侧时返回 False: ``V_a - V_b`` 这种左侧不是单一物理量,
    其量纲取决于两者是否同量纲, 静态求不出 —— 让调用方拒收, 而不是取首项。
    """
    import ast as _ast

    match node:
        case _ast.Name():
            res = dictionary.resolve(node.id, ns)  # type: ignore[attr-defined]
            if res.dimension is None:
                return False
            vec = [float(getattr(res.dimension, name)) for name in DIMENSION_ORDER]
            out.append((vec, node.id))
            return True
        case _ast.Constant() if isinstance(node.value, (int, float)):
            return True  # 常数无量纲贡献
        case _ast.BinOp() if isinstance(node.op, _ast.Mult | _ast.Div):
            sign = -1.0 if isinstance(node.op, _ast.Div) else 1.0
            mark = len(out)
            if not _walk_product(node.left, dictionary, ns, out):
                return False
            for i in range(mark, len(out)):
                vec, name = out[i]
                out[i] = ([v * sign for v in vec], name)
            return _walk_product(node.right, dictionary, ns, out)
        case _ast.BinOp() if isinstance(node.op, _ast.Pow):
            # 数值指数可静态求: ``I_rms**2`` -> dim(I_rms) * 2。
            # 指数是符号(``x**n``)时求不出, 拒收。
            if not isinstance(node.right, _ast.Constant):
                return False
            if not isinstance(node.right.value, int | float):
                return False
            power = float(node.right.value)
            mark = len(out)
            if not _walk_product(node.left, dictionary, ns, out):
                return False
            for i in range(mark, len(out)):
                vec, name = out[i]
                out[i] = ([v * power for v in vec], name)
            return True
    return False


def _resolve_prefixes(refs: tuple[str, ...], known: dict[str, FormulaRow]) -> tuple[str, ...]:
    """段前缀 -> 实际公式 ID。只认整串 / ``_`` / ``.`` 三种边界。

    免得 ``F_J.2`` 误配到 ``F_J.20_...``。
    """
    hits: list[str] = []
    for ref in refs:
        for fid in known:
            if fid == ref or fid.startswith(f"{ref}_") or fid.startswith(f"{ref}."):
                hits.append(fid)
                break
    return tuple(dict.fromkeys(hits))


def write_formula_csv(
    records: tuple[FormulaRecord, ...],
    path: Path,
) -> Path:
    """写 ``seed/formula.csv``。只写 ``records`` 里的, 绝不补行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for record in records:
            writer.writerow(record.as_csv_row())
    return path


def main(argv: list[str] | None = None) -> int:
    """CLI。**有记录被拒收时退出码非 0** —— 缺口不许被安静吞掉。"""
    parser = argparse.ArgumentParser(description="生成 seed/formula.csv(§18.6 步骤 1~7)")
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--out", type=Path, default=Path("seed/formula.csv"))
    args = parser.parse_args(argv)

    lines = read_spec(args.spec)
    records, report = build_records(lines)
    path = write_formula_csv(records, args.out)
    print(f"可入库 {len(records)} 条 -> {path}")
    print(f"过滤: {report.summary()}")
    if report.name_conflicts:
        print(f"方案名与标准名不一致 {len(report.name_conflicts)} 条(保留方案原文, 不自动改写):")
        for fid, dec, std, sid in report.name_conflicts[:8]:
            print(f"  {fid}: 方案={dec!r} 标准={std!r} ({sid})")
    if report.quarantined:
        print("隔离(不入库, 依据 docs/w1-triage.md):")
        for fid, why in sorted(report.quarantined.items()):
            print(f"  {fid}: {why[:96]}")
    for reason in sorted(report.reasons):
        print(f"  {reason}: {len(report.reasons[reason])} 条, 例 {report.reasons[reason][:3]}")
    if report.reasons:
        print("** 有拒收 -> 退出码 1。缺口须由决策解决, 不得靠编造填平。**")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
