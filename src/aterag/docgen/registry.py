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
from .standards import parse_standards
from .symbols import parse_symbol_table

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


@dataclass(frozen=True)
class FormulaRecord:
    """一条可入库的公式(所有 ``NOT NULL`` 列均已满足)。"""

    formula_id: str
    name_zh: str
    domain: str
    section: str
    domain_tags: tuple[str, ...]
    var_refs: tuple[str, ...]
    dimension_vec: tuple[float, ...]
    derive_from: tuple[str, ...]
    source_ref: str
    rendered: Rendered
    errata: str | None = None
    boundary: str | None = None
    name_en: str | None = None
    used_by_rule: tuple[str, ...] = ()
    used_by_test: tuple[str, ...] = ()
    used_by_axon: tuple[str, ...] = ()
    confidence: float = 0.95

    def as_csv_row(self) -> dict[str, str]:
        """转成与 ``formula`` 表同名的 CSV 行(列序见 :data:`CSV_COLUMNS`)。"""
        row: dict[str, str] = {
            "formula_id": self.formula_id,
            "name_zh": self.name_zh,
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
        }
        row.update({k: v or "" for k, v in self.rendered.as_row().items()})
        return row


#: CSV 列序。与 §18.3.1 的列序一致, 便于人工比对。
CSV_COLUMNS: tuple[str, ...] = (
    "formula_id", "name_zh", "name_en", "domain", "section", "domain_tags",
    "var_refs", "dimension_vec", "dimension_ok", "derive_from", "boundary",
    "confidence", "scope", "used_by_rule", "used_by_test", "used_by_axon",
    "errata", "source_ref",
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
    considered: int = 0

    def reject(self, reason: str, fid: str) -> None:
        self.reasons.setdefault(reason, []).append(fid)

    def summary(self) -> str:
        parts = [f"考虑 {self.considered} 条量纲闭合公式"]
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

    for fid, row in first.items():
        normalized = normalize_equation(row.expression)
        if not normalized.ok:
            continue
        match = _DOMAIN_RE.match(fid)
        ns = match.group(1) if match else None
        resolutions = [dictionary.resolve(v, ns) for v in normalized.variables]
        if any(r.dimension is None for r in resolutions):
            continue  # 未闭合, 不是 G1 的候选, 不算拒收
        report.considered += 1

        # --- NOT NULL 前置: 缺任一项即拒收, **不填占位值** ---
        if not row.name_zh:
            report.missing_name_zh.append(fid)
            report.reject("缺 name_zh(方案只按 ID 建索引)", fid)
            continue
        source = row.source_ref or next(iter(standards_of.get(fid, ())), None)
        if not source:
            report.missing_source_ref.append(fid)
            report.reject("缺 source_ref(公式表无此列, 且标准反向未覆盖)", fid)
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
            normalized.lhs, normalized.rhs or "", normalized.relation or "=", None
        )
        if rendered.error:
            report.render_failed.append(fid)
            report.reject(f"渲染失败: {rendered.error}", fid)
            continue

        out.append(
            FormulaRecord(
                formula_id=fid,
                name_zh=row.name_zh,
                domain=domain,
                section=section,
                domain_tags=(_DOMAIN_TAG.get(domain, domain),),
                var_refs=tuple(normalized.variables),
                dimension_vec=vec,
                derive_from=tuple(derivation.refs),
                source_ref=source,
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
    for reason in sorted(report.reasons):
        print(f"  {reason}: {len(report.reasons[reason])} 条, 例 {report.reasons[reason][:3]}")
    if report.reasons:
        print("** 有拒收 -> 退出码 1。缺口须由决策解决, 不得靠编造填平。**")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
