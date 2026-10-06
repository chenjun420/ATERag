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
REF_FIELDS = ("formula_refs", "axiom_refs", "source_id", "target_id")

#: 本库 id 命名空间形态(用于「值形似 id」的通用扫描)。
#: 推导自真实 id 集合: 750 条记录里 668 条带 id, 全部匹配此式(有测试钉)。
#: 有了这个式子, 将来新增字段(比如某个 ``related_refs``)忘了在
#: :data:`REF_FIELDS` 登记, 通用扫描仍能抓到它指不到目标 —— 声明式是
#: 主动检查, 通用扫描是兜底。
ID_NAMESPACE = re.compile(r"^(F_[A-Z]|A-\d|thm::|sym::|std::|load::|loadratio::|rel::)")

#: 标准号形态: ``GB/T 17626.5-2019`` / ``IEC 60664-1-2020`` / ``GB/Z 14429-2005``。
#: 用于「声明是 standard 却没给标准号」这类形态检查。
STANDARD_NUMBER = re.compile(r"^[A-Z]{2,4}(/([A-Z]|T|Z))?\s+\d")

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
    stray = [r for r in records if r.get("id") and not ID_NAMESPACE.match(str(r["id"]))]
    for r in stray[:10]:
        report.add(
            "id_namespace",
            "WARN",
            str(r.get("id")),
            "id 不匹配本库命名空间形态(通用扫描的判据因此变弱)",
        )
    return set(seen)


def check_refs(records: list[dict[str, Any]], ids: set[str], report: GateReport) -> None:
    """声明字段里的引用必须解析得到。"""
    for rec in records:
        rid = str(rec.get("id") or f"<no-id:{rec.get('entity_type')}>")
        for fld in REF_FIELDS:
            val = rec.get(fld)
            if not val:
                continue
            refs = val if isinstance(val, list) else [val]
            for ref in refs:
                if str(ref) not in ids:
                    report.add(
                        "ref_resolves", "ERROR", rid, f"{fld} -> {ref} 解析不到"
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
EXTERNAL_FIELDS = frozenset(
    {"text", "statement", "definition", "note", "standard_id", "theorem_id", "qudt_ref"}
)


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
            for v in val if isinstance(val, list) else [val]:
                if isinstance(v, str) and ID_NAMESPACE.match(v) and v not in ids:
                    hits[fld] += 1
    return dict(hits)


def check_undeclared_id_tokens(records: list[dict[str, Any]], ids: set[str], report: GateReport) -> None:
    """未登记字段里的 id 记号解析不到 -> **WARN 汇总**, 不逐条 ERROR。

    与声明字段分开定级是实测逼出来的: 同一种「解析不到」, 出现在
    `formula_refs` 里是确定的知识缺陷(该报错), 出现在方案 md 带过来的
    `upstream` 记号里则可能是「方案用的旧编号体系」而不是缺陷(该记录
    待查)。定级一致会同时制造漏报和误报。
    """
    for fld, n in sorted(discover_reference_fields(records, ids).items(), key=lambda kv: -kv[1]):
        report.add("undeclared_ref", "WARN", fld, f"{n} 条 id 记号解析不到(未登记为引用字段)")


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
    no_conf = sum(
        1
        for r in records
        if not _is_relation(r) and r.get("confidence") is None
    )
    report.add("confidence_present", "WARN", "-", f"{no_conf} 条未标 confidence")

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


def run_gate(records: list[dict[str, Any]]) -> GateReport:
    """跑全部门禁检查, 返回报告。**不修改任何数据**。"""
    report = GateReport()
    ids = check_ids(records, report)
    check_refs(records, ids, report)
    check_undeclared_id_tokens(records, ids, report)
    check_authority_shape(records, report)
    check_reportables(records, ids, report)
    by_type = Counter(str(r.get("entity_type")) for r in records)
    report.stats = {
        "records": len(records),
        "with_id": len(ids),
        "by_type": dict(by_type),
        "relations": sum(1 for r in records if _is_relation(r)),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="知识门: 种子记录集的质量门禁")
    ap.add_argument(
        "--seed",
        default="data/seed/power_domain_seed.json",
        help="种子 JSON 路径(默认 data/seed/power_domain_seed.json)",
    )
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    args = ap.parse_args(argv)

    seed_path = Path(args.seed)
    if not seed_path.is_file():
        print(f"种子文件不存在: {seed_path}", file=sys.stderr)
        return 2
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    report = run_gate(list(seed.get("records") or []))

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        st = report.stats
        print(f"知识门 {seed_path}: {st['records']} 条记录 ({st['with_id']} 带 id, {st['relations']} 关系)")
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
