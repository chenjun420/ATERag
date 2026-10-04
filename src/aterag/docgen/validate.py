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
    )

    for fid, row in first.items():
        normalized = normalize_equation(row.expression)
        if not normalized.ok:
            continue
        corpus.parseable[fid] = normalized.variables
        ns = _namespace_of(fid)
        unresolved = [
            v for v in normalized.variables if dictionary.resolve(v, ns).dimension is None
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


def gate_g1(corpus: Corpus) -> GateResult:
    """公式量纲闭合。

    G1 判据是 ``formula`` 表 ``dimension_ok=true`` **占比 100%** —— 它是对
    **已入库数据**的完整性校验, 不是覆盖率要求。所以:

    - 若分母取「已入库集」, 而入库集按定义只收闭合公式, 这个比值**恒等于
      100%** —— 那是个空转的 PASS, 比没有门禁更糟(正是本项目在
      ``test_corpus_fingerprint`` 里明令禁止的那类断言)。
    - 而 ``formula`` 表尚不存在(``registry.py`` 未实现), 无从校验。

    故本阶段报 ``NOT_APPLICABLE``, 并把三个分母下的覆盖率并列输出 ——
    覆盖率的正式判据是 §18.4.2 判据 1(D3 已改为对照可推导覆盖率), 不是 G1。
    """
    total = len(corpus.first)
    parseable = len(corpus.parseable)
    closed = len(corpus.closed)
    derivable = len(corpus.derivable)
    top = ", ".join(f"{s}×{c}" for s, c in corpus.blockers.most_common(8))
    return GateResult(
        gate_id="G1",
        title="公式量纲闭合",
        level=Level.MERGE_BLOCK,
        status=Status.NOT_APPLICABLE,
        detail=(
            f"formula 表尚未生成(registry.py 未实现), G1 无从校验; "
            f"若分母取已入库集则恒为 100%, 属空转断言, 故不报 PASS。"
            f"覆盖率 分母全部{total}={_pct(closed, total)} "
            f"分母可解析{parseable}={_pct(closed, parseable)} "
            f"分母可推导{derivable}={_pct(closed, derivable)}"
        ),
        evidence=(
            f"闭合 {closed} / 可解析 {parseable} / 全部 {total}",
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
    """公理可追溯: ``derive_from`` 非空或标「实验定律」。"""
    exp = "实验定律"
    missing: list[str] = []
    marked = 0
    for fid in corpus.closed:
        upstream = corpus.first[fid].upstream
        if any(exp in item for item in upstream):
            marked += 1
        elif not upstream:
            missing.append(fid)
    status = Status.PASS if not missing else Status.FAIL
    note = f"; 其中标「{exp}」的 {marked} 条" if marked else f"; 无一条标「{exp}」"
    return GateResult(
        gate_id="G4",
        title="公理可追溯",
        level=Level.MERGE_BLOCK,
        status=status,
        detail=(
            f"{len(corpus.closed) - len(missing)}/{len(corpus.closed)} "
            f"条闭合公式有 derive_from{note}; 缺 {len(missing)} 条"
        ),
        evidence=tuple(missing[:12]),
    )


def gate_g5(corpus: Corpus) -> GateResult:
    """勘误同步: 7 条勘误公式全部带 ``errata``。

    关联事实实在 ``Erratum.formula_refs`` 与 ``link_note``, **不在**
    ``FormulaRow.errata`` —— 实测该字段 545 行**全为空**, 读它会得到
    「0/7」的假失败。``link_note`` 标明来源类别(正文点名 / 两跳推导 / 声明),
    一并输出以便审计区分「有文档支撑」与「我方补的」。
    """
    lines: list[str] = []
    unresolved: list[str] = []
    for item in corpus.errata:
        tag = str(getattr(item, "tag", "?"))
        refs = tuple(getattr(item, "formula_refs", ()) or ())
        note = str(getattr(item, "link_note", "") or "")
        hits = _resolve_refs(refs, corpus.first)
        if not hits:
            unresolved.append(tag)
        lines.append(f"{tag} [{note}] {len(hits)} 条: {', '.join(hits[:3])}")
    status = Status.PASS if not unresolved and len(corpus.errata) == 7 else Status.FAIL
    return GateResult(
        gate_id="G5",
        title="勘误同步",
        level=Level.MERGE_BLOCK,
        status=status,
        detail=(
            f"{len(corpus.errata) - len(unresolved)}/7 条勘误关联到实际公式"
            + (f"; 未解析: {', '.join(unresolved)}" if unresolved else "")
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
            f"正向不可判定(FormulaRow.source_ref 545 行全为空, 实测 {len(forward)} 个)"
        ),
        evidence=tuple(dangling[:10]),
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
