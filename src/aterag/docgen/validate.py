"""§18.9 质量门禁 G1~G10。

§18.10 注 5 指出方案自身的失败模式是「有生成器但无门禁」—— 生成器只把手工维护的
CSV 变成 SQL, 拦住「量纲不齐的公式进了库」的是门禁。所以本模块的判据**照抄
§18.9 原文**, 不自行放宽:

===========  ==================================================  ==============
门禁          判据                                                阻断级别
===========  ==================================================  ==============
G1          ``formula.dimension_ok=true`` 占比 100%                合并阻断
G2          R1–R20 + P1–P18 的 ``formula_ref`` 全部存在              合并阻断
G3          G.1–G.40 的 ``judge_formula`` 全部存在                  合并阻断
G4          ``formula.derive_from`` 非空或标「实验定律」             合并阻断
G5          7 条勘误公式全部带 ``errata``                         合并阻断
G6          ``formula.source_ref`` ⊆ ``standards_registry``        警告
G7          无裸符号歧义(U.5 全覆盖)                              警告
G8          ``retrieval.formula`` 返回 ``FormulaCard`` 比例 100%   发布阻断
G9          必测点位映射覆盖率 100%                                 发布阻断
G10         附录数声明一致; 短引用 0 悬空; 代码围栏全闭合            发布阻断
===========  ==================================================  ==============

## 为什么需要 ``NOT_APPLICABLE`` 这个状态

G2/G3 依赖 W2 的规则、G8 依赖 W3 的检索、G9 依赖 W6 的通道映射 —— 在 W1 阶段
它们**无法判定**。若此时报 PASS, 就是**假绿灯**: 比红灯更危险, 因为它让人以为
下游已就绪。故未实现的门禁一律报 ``NOT_APPLICABLE`` 并写明依赖, 且**不计入
退出码**。

## G1 与 §18.4.2 判据 1 是两件事

G1 是**比率**, 只约束**已入库**的那批公式 —— 只入库闭合公式即可 100%。
判据 1 是**里程碑条数**(原为 ≥400), 是 W1 出口判据而非 CI 门禁。
两者混为一谈会让人以为 G1 被 400 卡住, 实际并没有。

因此 G1 额外报出**三个分母**下的覆盖率, 而不是只报一个好看的数字:

- 545 —— 全部公式 ID
- 369 —— 可解析(引擎语法通过)
- 可推导范围 —— 可解析且**不含**刻意不给规则的歧义词干(真正的「该由我闭合」)

第三个才是判据的正式分母; 前两个并列显示, 便于区分「被**歧义**挡住」与
「被**覆盖缺口**挡住」。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .derive import EMPIRICAL_PENDING, Derivation, derive_all, summarize
from .errata import parse_errata
from .expr_norm import normalize_equation
from .quantity_rules import build_dictionary
from .spec_parse import FormulaRow, parse_formula_rows, read_spec
from .standards import parse_standards
from .symbols import SymbolTable, parse_symbol_table

__all__ = [
    "GateResult",
    "Level",
    "Status",
    "SPEC_PATH",
    "main",
    "run_gates",
]

SPEC_PATH = (
    Path(__file__).resolve().parents[3]
    / "定制电源产品转产工装研发系统_开发指导方案_V6.0.md"
)

#: 公式 ID 里的域字母: ``F_J.3.1_INDUCTOR_RIPPLE`` -> ``J``。
#: 量纲解析键是 **(符号, 命名空间)**, 必须先取命名空间, 否则规则表会跨域误命中。
_DOMAIN_RE = re.compile(r"F_([A-Z])(?=[._])")

#: §18.10 与 ``quantity_rules._DELIBERATELY_UNRULED`` 一致: 这些词干是
#: **故意**不给规则的(方案自身歧义或矛盾)。出现在阻塞变量里时, 该公式不属于
#: 「可推导范围」—— 它卡住不是覆盖缺口, 而是方案没给出足够信息。
_DELIBERATELY_UNRULED = frozenset({"T", "A", "H", "U", "u", "s", "z", "y", "e", "k", "x"})


class Status(Enum):
    """门禁结论。"""

    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    #: 依赖尚未就绪, **无法判定**。绝不用 PASS 代替。
    NOT_APPLICABLE = "N/A"


class Level(Enum):
    MERGE_BLOCK = "合并阻断"
    WARNING = "警告"
    RELEASE_BLOCK = "发布阻断"


@dataclass(frozen=True)
class GateResult:
    """一条门禁结论。

    ``evidence`` 放**可核查的明细**(具体 ID、具体符号), 不放结论性描述 ——
    门禁的意义就是让人能顺着 evidence 复核。
    """

    gate_id: str
    title: str
    level: Level
    status: Status
    detail: str
    evidence: tuple[str, ...] = ()

    @property
    def blocks_merge(self) -> bool:
        return self.status is Status.FAIL and self.level is Level.MERGE_BLOCK


@dataclass
class Corpus:
    """一次读完方案, 供所有门禁复用 —— 22,919 行不便宜。"""

    lines: list[str] = field(repr=False)
    rows: tuple[FormulaRow, ...] = ()
    first: dict[str, FormulaRow] = field(default_factory=dict, repr=False)
    parseable: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    closed: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    blockers: Counter[str] = field(default_factory=Counter, repr=False)
    derivable: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    symbols: SymbolTable | None = None
    standards: tuple[object, ...] = ()
    errata: tuple[object, ...] = ()
    axiom_rows: int = 0
    #: D2 的 ``derive_from`` 补全结果(含来源标记)。见 :mod:`docgen.derive`。
    derivations: dict[str, Derivation] = field(default_factory=dict, repr=False)
    #: 归一化后的左右两侧文本(供齐次性检查)。**不是**全部公式都有 —— 只有
    #: 通过归一化的才有。
    lhs_text: dict[str, str] = field(default_factory=dict, repr=False)
    rhs_text: dict[str, str] = field(default_factory=dict, repr=False)


def _namespace_of(formula_id: str) -> str | None:
    match = _DOMAIN_RE.match(formula_id)
    return match.group(1) if match else None


def load_corpus(spec: Path = SPEC_PATH) -> Corpus:
    """读方案并算出各门禁要用的中间量。"""
    lines = read_spec(spec)
    rows = parse_formula_rows(lines).rows

    # 每个 ID 只取第一段: 一个单元格里常有多条公式(反引号分隔), 逐条计数会
    # 把「公式条数」与「公式 ID 数」混为一谈。
    first: dict[str, FormulaRow] = {}
    for row in rows:
        first.setdefault(row.formula_id, row)

    symbols = parse_symbol_table(lines)[0]
    dictionary = build_dictionary(symbols)
    errata_report = parse_errata(lines)
    standards_report = parse_standards(lines)

    corpus = Corpus(
        lines=lines,
        rows=rows,
        first=first,
        symbols=symbols,
        standards=standards_report.records,
        errata=errata_report.errata,
        axiom_rows=len(errata_report.axiom_rows),
        derivations=derive_all(first),
    )

    for fid, row in first.items():
        normalized = normalize_equation(row.expression)
        if not normalized.ok:
            continue
        corpus.parseable[fid] = normalized.variables
        if normalized.lhs:
            corpus.lhs_text[fid] = normalized.lhs
        if normalized.rhs:
            corpus.rhs_text[fid] = normalized.rhs
        ns = _namespace_of(fid)
        unresolved = [
            v for v in normalized.variables if dictionary.resolve(v, ns, fid).dimension is None
        ]
        if not unresolved:
            corpus.closed[fid] = normalized.variables
            continue
        corpus.blockers[unresolved[0]] += 1
        # 「可推导」= 能解析, 且阻塞词**不是**刻意不给规则的那些。
        # 卡在 s/k/e/T 上的公式属于方案歧义, 不该算进我方的覆盖缺口。
        if unresolved[0] not in _DELIBERATELY_UNRULED:
            corpus.derivable[fid] = normalized.variables

    return corpus


def _pct(part: int, whole: int) -> str:
    return "n/a" if whole == 0 else f"{part / whole * 100:.1f}%"


# ---------------------------------------------------------------------------
# 门禁实现
# ---------------------------------------------------------------------------


@dataclass
class HomogeneityReport:
    """量纲**齐次性**检查结果 —— 这才是 §18.9 G1 要的东西。

    此前判「闭合」只验「每个符号都能查到量纲」, 那是**符号有定义**, 不是
    **方程自洽**。§18.10 注 3 明写「量纲校验是 R1 的机器实现, 必须在入库阶段
    拦截」—— 而 :func:`solver.symbolic.check_expression` 早就在仓库里, 却从没
    被 docgen 调用过。接线后实测:129 条里只有 **108** 条真正齐次。

    三档分流而非通过/失败, 因为成因不同、补救方式也不同:

    ``homogeneous``
        两侧量纲相等, ``dimension_ok`` 可为 true。
    ``inhomogeneous``
        两侧不等。成因**混合**(方案公式错 / 我方符号规则错 / LHS 认错),
        必须逐条看 —— 不能一股脑归给方案。
    ``unsupported``
        引擎**判不了**(用户自定义函数 ``η(V_in)``、连乘 ``Π`` 等), 这是引擎
        能力缺口, 不是公式错。
    """

    homogeneous: list[str] = field(default_factory=list)
    inhomogeneous: list[tuple[str, str, str]] = field(default_factory=list)
    unsupported: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"齐次 {len(self.homogeneous)} / 不齐次 {len(self.inhomogeneous)} / "
            f"引擎判不了 {len(self.unsupported)}"
        )


def check_homogeneity(corpus: Corpus) -> HomogeneityReport:
    """对每条符号全可解析的公式跑 :func:`solver.symbolic.check_expression`。

    LHS 量纲取左侧的静态乘积(纯乘除幂)—— 带加减的左侧静态求不出, 记入
    ``unsupported``, **不猜**。
    """
    from ..solver.symbolic import (
    Dimension,
    ExpressionError,
    VariableSpec,
    check_expression,
)
    from .quantity_rules import build_dictionary
    from .registry import DIMENSION_ORDER, _lhs_dimension

    report = HomogeneityReport()
    if corpus.symbols is None:
        return report
    dictionary = build_dictionary(corpus.symbols)
    for fid, variables in corpus.closed.items():
        specs = {
            v: VariableSpec(name=v, dimension=d)
            for v, d in ((v, dictionary.resolve(v, _ns_of(fid), fid).dimension) for v in variables)
            if d is not None
        }
        lhs_vec = _lhs_dimension(corpus.lhs_text.get(fid), dictionary, _ns_of(fid))
        # ``_lhs_dimension`` 返回 7 元列表(为 CSV 方便), 而 check_expression
        # 要的是 ``Dimension``。在这里转回去, 顺序由 DIMENSION_ORDER 固定。
        lhs = Dimension(**dict(zip(DIMENSION_ORDER, lhs_vec))) if lhs_vec else None
        rhs = corpus.rhs_text.get(fid)
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


def _ns_of(formula_id: str) -> str | None:
    match = _DOMAIN_RE.match(formula_id)
    return match.group(1) if match else None


def gate_g1(corpus: Corpus) -> GateResult:
    """公式量纲齐次(G1)。

    §18.9 G1 判据是 ``formula.dimension_ok=true`` 占比 100%。``dimension_ok``
    的语义是**方程量纲自洽**, 故本门禁跑真正的齐次性检查, 而不是「符号有定义」。

    分母是「符号全可解析」的候选集 —— 它们才是可能入库的那批。
    """
    report = check_homogeneity(corpus)
    total = len(corpus.closed)
    good = len(report.homogeneous)
    ratio = _pct(good, total)
    # 合并阻断: 只要有候选不齐次或判不了, 就不许入库 —— 因为「判不了」意味着
    # 我们**没有验证过**, 而入库即宣称已验证。
    status = Status.PASS if not report.inhomogeneous and not report.unsupported else Status.FAIL
    top = ", ".join(f"{s}×{c}" for s, c in corpus.blockers.most_common(6))
    return GateResult(
        gate_id="G1",
        title="公式量纲齐次",
        level=Level.MERGE_BLOCK,
        status=status,
        detail=(
            f"{good}/{total} = {ratio} 的候选公式量纲齐次。"
            f"不齐次 {len(report.inhomogeneous)} 条、引擎判不了 "
            f"{len(report.unsupported)} 条(用户自定义函数/连乘等)。"
            f"覆盖率 分母全部{len(corpus.first)}={_pct(good, len(corpus.first))} "
            f"分母可解析{len(corpus.parseable)}={_pct(good, len(corpus.parseable))} "
            f"分母可推导{len(corpus.derivable)}={_pct(good, len(corpus.derivable))}"
        ),
        evidence=(
            "不齐次明细(成因混合, 需逐条判): "
            + "; ".join(f"{f}={r[:40]}" for f, _e, r in report.inhomogeneous[:4]),
            "引擎判不了: " + "; ".join(f"{f}({r[:36]})" for f, r in report.unsupported[:4]),
            f"主要阻塞符号: {top}",
        ),
    )


def _resolve_refs(refs: tuple[str, ...], known: dict[str, FormulaRow]) -> tuple[str, ...]:
    """把 ``F_J.2`` 这类**段前缀**解析成实际存在的公式 ID。

    勘误与标准引用用的是段前缀(``F_J.2``), 而公式 ID 是 ``F_J.2.1_BUCK``,
    所以必须做前缀匹配。只认三种边界(整串 / ``_`` / ``.``), 免得 ``F_J.2``
    误配到 ``F_J.20_...``。
    """
    hits: list[str] = []
    for ref in refs:
        for fid in known:
            if fid == ref or fid.startswith(f"{ref}_") or fid.startswith(f"{ref}."):
                hits.append(fid)
                break
    return tuple(dict.fromkeys(hits))


def gate_g4(corpus: Corpus) -> GateResult:
    """公理可追溯: ``derive_from`` 非空或标「实验定律」。

    补全策略见 :mod:`docgen.derive`(D2 决定)。门禁**只判「有没有」**, 不判
    「对不对」—— 但必须把「我们补的」与「方案写的」分开报, 否则一个全绿会
    掩盖 35 条其实从未被核实。
    """
    derivations = corpus.derivations or {}
    pending = sorted(f for f in corpus.closed if (d := derivations.get(f)) and d.review_required)
    inherited = sorted(f for f in corpus.closed if (d := derivations.get(f)) and d.is_inherited)
    uncovered = sorted(f for f in corpus.closed if f not in derivations)
    # §18.9 G4 原文是「``derive_from`` **非空(或标注「实验定律」)**」。
    # 「实验定律(待复核)」是后者的标注形态, 故它**满足**判据 —— 本门禁只在
    # 「连一个标注都没有」时 FAIL。
    # 「待复核」不静默放过: 数量进 detail、ID 进 evidence, 且它是 W1 出口前
    # 必须清掉的欠账(见 docs/w1-status.md)。
    status = Status.FAIL if uncovered else Status.PASS
    by_prov = summarize(derivations) if derivations else Counter()
    return GateResult(
        gate_id="G4",
        title="公理可追溯",
        level=Level.MERGE_BLOCK,
        status=status,
        detail=(
            f"{len(corpus.closed) - len(uncovered)}/{len(corpus.closed)} "
            f"条闭合公式有 derive_from。来源: "
            f"方案明写 {by_prov.get('explicit', 0)}、"
            f"同章继承 {by_prov.get('inherited', 0)}、"
            f"标「{EMPIRICAL_PENDING}」 {by_prov.get('empirical-pending-review', 0)}"
            + (f"; 未覆盖 {len(uncovered)} 条" if uncovered else "")
        ),
        evidence=(
            f"继承自同章兄弟: {len(inherited)} 条(章内取值一致方可继承)",
            f"**待复核 {len(pending)} 条** —— 标为实验定律但未经核实, "
            f"W1 出口前需逐条确认",
        )
        + tuple(pending[:10]),
    )


def gate_g5(corpus: Corpus) -> GateResult:
    """勘误同步: 7 条勘误指向的公式**必须真的进了库**。

    关联事实实在 ``Erratum.formula_refs`` 与 ``link_note``, **不在**
    ``FormulaRow.errata`` —— 实测该字段 545 行**全为空**, 读它会得到
    「0/7」的假失败。

    ## 分母必须是「已入库集」, 不是全语料

    早先一版拿 :attr:`Corpus.first` 当分母, 于是每条勘误都能在全语料里找到
    目标, 报出 ``7/7 PASS`` —— 而 ``seed/formula.csv`` 里当时只有 **2** 条
    带 ``errata``。15 个目标因为齐次性门禁或符号未闭合被拒收, 于是「勘误已同步」
    这个绿灯是**空转断言**: 它度量的是「方案里提到过」, 不是「知识库里查得到」。

    勘误是全套数据里最要命的一类标注 —— 它说的是「这条式子有个常见错法」。
    目标公式没入库, 这条警告就在库里彻底消失, 而下游看到的仍是绿灯。所以这里
    改用 :func:`aterag.docgen.registry.build_records` 的**实际产物**当分母,
    而不是读 ``seed/formula.csv``(那可能是上一次的陈旧文件)。
    """
    from .registry import build_records

    ingested = {record.formula_id for record in build_records(corpus.lines)[0]}
    lines: list[str] = []
    unresolved: list[str] = []
    partial: list[str] = []
    covered = 0
    for item in corpus.errata:
        tag = str(getattr(item, "tag", "?"))
        refs = tuple(getattr(item, "formula_refs", ()) or ())
        note = str(getattr(item, "link_note", "") or "")
        hits = _resolve_refs(refs, corpus.first)
        if not hits:
            unresolved.append(tag)
            lines.append(f"{tag} [{note}] 0 条: 语料里就没有目标公式")
            continue
        in_kb = [f for f in hits if f in ingested]
        covered += len(in_kb)
        if not in_kb:
            partial.append(f"{tag}(0/{len(hits)})")
        elif len(in_kb) < len(hits):
            partial.append(f"{tag}({len(in_kb)}/{len(hits)})")
        lines.append(
            f"{tag} [{note}] 目标 {len(hits)} 条, 入库 {len(in_kb)} 条: "
            f"{', '.join(in_kb[:3]) or '(无)'}"
        )
    # 只要有勘误的目标没进库, 就**不算同步** —— 警告随公式一起丢了。
    status = Status.PASS if not unresolved and not partial and len(corpus.errata) == 7 else Status.FAIL
    return GateResult(
        gate_id="G5",
        title="勘误同步",
        level=Level.MERGE_BLOCK,
        status=status,
        detail=(
            f"{len(corpus.errata) - len(unresolved) - len(partial)}/{len(corpus.errata)} "
            f"条勘误的全部目标已入库(共 {covered} 个目标公式)。"
            + (f" 未入库: {', '.join(partial)}" if partial else "")
            + (f" 语料无目标: {', '.join(unresolved)}" if unresolved else "")
        ),
        evidence=tuple(lines),
    )


def gate_g6(corpus: Corpus) -> GateResult:
    """标准引用已登记: ``formula.source_ref`` ⊆ ``standards_registry``。

    方案原文的方向是「公式的 ``source_ref`` 要在 registry 里」。但实测
    ``FormulaRow.source_ref`` **545 行全为空** —— 公式表里根本没有这一列,
    所以正向检查是空转的。改为报**可判定的反向**:标准条目引用的公式是否存在,
    并明确指出正向不可判定, 不拿反向结果冒充正向通过。
    """
    known_ids = {str(getattr(r, "standard_id", "")) for r in corpus.standards}
    known_ids.discard("")
    forward = {
        str(corpus.first[fid].source_ref)
        for fid in corpus.closed
        if corpus.first[fid].source_ref
    }
    dangling: list[str] = []
    bound = 0
    for record in corpus.standards:
        hits = _resolve_refs(
            tuple(getattr(record, "formula_refs", ()) or ()), corpus.first
        )
        if hits:
            bound += 1
        else:
            sid = str(getattr(record, "standard_id", "?"))
            refs = tuple(getattr(record, "formula_refs", ()) or ())
            if refs:
                dangling.append(f"{sid} → {', '.join(refs[:3])}")
    status = Status.WARN if (dangling or not forward) else Status.PASS
    return GateResult(
        gate_id="G6",
        title="标准引用已登记",
        level=Level.WARNING,
        status=status,
        detail=(
            f"已登记标准 {len(known_ids)} 条, 其中 {bound} 条能解析到公式; "
            f"{len(dangling)} 条引用悬空; "
            f"正向不可判定(FormulaRow.source_ref 在 {len(corpus.first)} 行里只有 "
            f"{len(forward)} 个非空 —— 公式表**没有**这一列)"
        ),
        # evidence 恒非空: 即使无悬空也要说明「查了什么」, 否则「无 evidence」
        # 与「没查」在报告上长得一样。
        evidence=tuple(dangling[:10])
        or (f"无悬空: {bound} 条标准的 formula_refs 全部解析到公式",),
    )


def gate_g7(corpus: Corpus) -> GateResult:
    """命名空间隔离: 无裸符号歧义。"""
    table = corpus.symbols
    by_symbol: dict[str, set[str]] = {}
    if table is not None:
        for entry in table.entries:
            sym = str(entry.symbol)
            dim = str(entry.dimension)
            by_symbol.setdefault(sym, set()).add(dim)
    ambiguous = sorted(sym for sym, dims in by_symbol.items() if len(dims) > 1)
    explicit = (
        sorted(
            str(e.symbol)
            for e in (table.entries if table is not None else ())
            if getattr(e, "ambiguous", False)
        )
    )
    status = Status.PASS if not ambiguous and not explicit else Status.WARN
    return GateResult(
        gate_id="G7",
        title="命名空间隔离",
        level=Level.WARNING,
        status=status,
        detail=(
            f"U.5 符号 {len(by_symbol)} 个; 跨命名空间量纲冲突 {len(ambiguous)} 个, "
            f"U.5 显式标歧义 {len(explicit)} 个 —— 均按 (符号,命名空间) 解析, 不做前缀回退"
        ),
        evidence=tuple(ambiguous[:10]) + tuple(f"显式: {s}" for s in explicit[:6]),
    )


def gate_g10(corpus: Corpus) -> GateResult:
    """文档一致性: 代码围栏全闭合 + 附录数声明与实际一致。

    这一项顺带**反证抽取器本身**: 代码围栏若不闭合, 全文按行切分的抽取就可能
    读到错位的表格 —— 那会让前面所有计数都建立在错位的输入上。
    """
    fences = [i for i, line in enumerate(corpus.lines, 1) if line.lstrip().startswith("```")]
    unbalanced = len(fences) % 2 == 1

    declared: re.Match[str] | None = None
    appendix_re = re.compile(r"^##\s*附录\s*([A-Z])")
    for line in corpus.lines:
        if "共" in line and "个附录" in line:
            declared = re.search(r"共\s*(\d+)\s*个附录", line)
            if declared:
                break
    actual = sum(1 for line in corpus.lines if appendix_re.match(line))

    problems: list[str] = []
    if unbalanced:
        problems.append(f"代码围栏数 {len(fences)} 为奇数, 未闭合")
    if declared and int(declared.group(1)) != actual:
        problems.append(f"导览声明 {declared.group(1)} 个附录, 实际 {actual} 个")
    status = Status.PASS if not problems else Status.FAIL
    # 如实说明哪几项子判据**没有跑**, 免得一个 PASS 被当成「文档一致性已全查」。
    unchecked: list[str] = []
    if not declared:
        unchecked.append("附录数声明(全文未找到「共 N 个附录」句式, 无法比对)")
    unchecked.append("短引用悬空(未实现: 需要先定「短引用」的语法范围)")
    return GateResult(
        gate_id="G10",
        title="文档一致性",
        level=Level.RELEASE_BLOCK,
        status=status,
        detail=(
            f"代码围栏 {len(fences)} 个({'成对' if not unbalanced else '未闭合'}); "
            f"实际附录 {actual} 个; "
            f"**{len(unchecked)} 项子判据未跑**"
        ),
        evidence=(*problems, *(f"未跑: {u}" for u in unchecked)),
    )


def _pending(gate_id: str, title: str, level: Level, depends: str) -> GateResult:
    """依赖未就绪的门禁。

    **绝不用 PASS 代替** —— 假绿灯比红灯危险, 它让人以为下游已就绪。
    """
    return GateResult(
        gate_id=gate_id,
        title=title,
        level=level,
        status=Status.NOT_APPLICABLE,
        detail=f"依赖未就绪, 本阶段无法判定({depends})",
        evidence=(f"需先完成: {depends}",),
    )


_GATES: dict[str, tuple[str, Level, str]] = {
    "G2": ("规则公式依据非空", Level.MERGE_BLOCK, "W2 规则表 R1–R20 / P1–P18 的 formula_ref"),
    "G3": ("测试判据公式存在", Level.MERGE_BLOCK, "W2 测试判据 G.1–G.40 的 judge_formula"),
    "G8": ("检索无裸文本", Level.RELEASE_BLOCK, "W3 retrieval.formula 返回 FormulaCard"),
    "G9": ("通道映射完整", Level.RELEASE_BLOCK, "W6 必测点位映射"),
}


def run_gates(ids: list[str] | None = None, spec: Path = SPEC_PATH) -> list[GateResult]:
    """跑指定门禁(``None`` = 全部), 返回结论列表。"""
    corpus = load_corpus(spec)
    implemented = {
        "G1": gate_g1,
        "G4": gate_g4,
        "G5": gate_g5,
        "G6": gate_g6,
        "G7": gate_g7,
        "G10": gate_g10,
    }
    chosen = list(ids) if ids else [*implemented, *_GATES]
    results: list[GateResult] = []
    for gid in chosen:
        key = gid.upper()
        if key in implemented:
            results.append(implemented[key](corpus))
        elif key in _GATES:
            title, level, depends = _GATES[key]
            results.append(_pending(key, title, level, depends))
        else:
            raise SystemExit(f"未知门禁 {gid!r}; 可用: {sorted([*implemented, *_GATES])}")
    return results


def _render(results: list[GateResult]) -> str:
    lines = ["", "§18.9 质量门禁", "=" * 68]
    for r in results:
        mark = {"PASS": "OK  ", "FAIL": "FAIL", "WARN": "WARN", "N/A": "--  "}[r.status.value]
        lines.append(f"[{mark}] {r.gate_id:<4} {r.title:<12} ({r.level.value})")
        lines.append(f"         {r.detail}")
        for item in r.evidence:
            lines.append(f"         - {item}")
    blocking = [r for r in results if r.blocks_merge]
    failing = [r for r in results if r.status is Status.FAIL]
    lines.append("=" * 68)
    pending = [r.gate_id for r in results if r.status is Status.NOT_APPLICABLE]
    lines.append(
        f"合并阻断失败 {len(blocking)} 项; 总计失败 {len(failing)} 项; "
        f"未就绪 {len(pending)} 项({','.join(pending) if pending else '无'})"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``--all`` 或 ``--gates G1 G2``。有合并阻断项失败则退出码非 0。"""
    parser = argparse.ArgumentParser(description="§18.9 质量门禁 G1~G10")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="全部 10 项")
    group.add_argument("--gates", nargs="+", metavar="G", help="指定门禁, 如 G1 G2")
    parser.add_argument("--spec", type=Path, default=SPEC_PATH, help="方案文件路径")
    args = parser.parse_args(argv)

    results = run_gates(None if args.all else list(args.gates), args.spec)
    print(_render(results))
    return 1 if any(r.blocks_merge for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
