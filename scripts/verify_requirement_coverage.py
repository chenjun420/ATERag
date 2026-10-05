"""4.3 功能/性能要求 章节的需求覆盖对账 (完整性证明, 不是抽样).

要回答"抽取该章节下所有非'不要求/无要求'的需求", 必须证明**每一行都被归类**,
不能靠"抽到了 N 条"就宣称完整。本脚本输出逐行对账台账:

  表格行 155 = 需求行 147 + 属性行 4 (通信协议基本参数) + 参考表行 4 (空开选型)
  需求行 147 = 保留 94 + 剔除 53          <- 剔除逐条带原因与命中词
  散文块   2 = 跨章节引用 1 + 明示"无要求" 1

任一侧对不上即 FAIL —— 有行被静默丢弃时这里会直接暴露。

与 MCP 服务的 stats 交叉核对 (rows_total / kept / excluded),
确保对账依据与线上服务读的是同一份数据。

用法:
  板卡:  /opt/aterag/.venv/bin/python scripts/verify_requirement_coverage.py
  本机:  .venv\\Scripts\\python.exe scripts/verify_requirement_coverage.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if (APP_ROOT / "src").is_dir():
    os.chdir(APP_ROOT)
    sys.path.insert(0, str(APP_ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from aterag.extract import (  # noqa: E402
    ProfileBook,
    apply_sieve,
    load_annotations,
    load_blocks,
    rows_from_blocks,
    select_sections,
)
from aterag.ingest.table_schema import load_registry  # noqa: E402

MODEL = "PA601-D54A"
DOC = "PA601-D54A 定制电源技术规格书.md"


def _prose_words(prof) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """散文判别措辞取自档案; 档案未声明则回落到空 (判别结果记为待审, 不静默放行)。"""
    from aterag.extract.resolve import ReferenceSpec

    spec = ReferenceSpec(markers=prof.reference_markers, req_id_pattern=prof.req_id_pattern)
    crossref = spec.markers if spec.enabled else ()
    return crossref, prof.exclude_words


PASS, FAIL = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" | {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp", default="", help="板卡 MCP 地址 (如 http://192.168.5.25:8080/mcp)")
    ap.add_argument("--mcp-stats", default="", help="直接给一段 MCP 返回的 JSON 文件, 跳过网络")
    args = ap.parse_args()

    reg = load_registry()
    blocks = load_blocks(MODEL)
    prof = ProfileBook.load().get("power_spec_cn")
    sel = select_sections(blocks, prof.section_keywords)
    prefixes = sel.section_prefixes
    print(f"=== 4.3 需求覆盖对账 ({MODEL}) ===")
    print(f"章节关键字 {list(prof.section_keywords)} -> 前缀 {prefixes}\n")

    # ---------- 1. 表格行普查: 每一行按 schema 归类 ----------
    print("--- 1. 表格行普查 (每一行必须归入下列某一类) ---")
    cls = Counter()  # 分类 -> 行数
    by_schema: dict[str, list[str]] = defaultdict(list)
    for b in blocks:
        if b.section_path not in prefixes and not any(
            b.section_path.startswith(p + ".") for p in prefixes
        ):
            continue
        for t in b.tables:
            if not t:
                continue
            det = reg.detect(t[0], t[1:])
            n = len(t) - 1
            if not det.matched:
                kind = "缺口(未映射)"
            elif det.schema.entity == "requirement":
                kind = "需求行"
            elif det.schema.entity == "attribute":
                kind = "属性行"
            elif det.schema.entity == "none":
                kind = "参考表" if det.schema.reference else "元数据行"
            else:
                kind = f"其它({det.schema.entity})"
            cls[kind] += n
            by_schema[f"{b.section_path} {det.signature[:56]}"].append(f"{kind} x{n}")
    total_rows = sum(cls.values())
    for k, v in cls.most_common():
        print(f"    {k:<14} {v:>4} 行")
    print(f"    {'表格行合计':<14} {total_rows:>4} 行")
    check(
        "1-普查无未映射缺口",
        cls.get("缺口(未映射)", 0) == 0,
        f"缺口 {cls.get('缺口(未映射)', 0)} 行",
    )

    # ---------- 2. 需求行: 保留 + 剔除 = 全部 ----------
    print("\n--- 2. 需求行拆分 (剔除必须有原因) ---")
    rows, review, _ = rows_from_blocks(blocks, MODEL, "B", prefixes)
    out = apply_sieve(rows, prof.exclude_words)
    n_req, n_keep, n_excl = len(rows), len(out.kept), len(out.excluded)
    print(f"    需求行 {n_req} = 保留 {n_keep} + 剔除 {n_excl}")
    check(
        "2-需求行数与普查一致",
        n_req == cls.get("需求行", 0),
        f"抽取 {n_req} vs 普查 {cls.get('需求行', 0)}",
    )
    check("2-保留+剔除=需求行", n_keep + n_excl == n_req, f"{n_keep}+{n_excl}={n_req}")
    check("2-每条剔除都有原因", all(e.reason and e.matched_word for e in out.excluded))
    by_field = Counter(e.field for e in out.excluded)
    print(f"    剔除来源: {dict(by_field)}  剔除词: {list(prof.exclude_words)}")
    check(
        "2-剔除仅来自等级/备注整格",
        set(by_field) <= {"priority", "notes"},
        str(dict(by_field)),
    )
    # 备注含剔除词子串但等级=强制 -> 必须保留
    kept_ids = {r["req_id"] for r in out.kept}
    check("2-等级=强制未被误剔", "SR-PA601-D54A-1219" in kept_ids, "SR-1219 备注含'不要求'子串")

    # ---------- 3. 剔除项按需求编号聚合 (便于人工核对) ----------
    print("\n--- 3. 被剔除的需求编号 (按 SR 号) ---")
    excl_ids: dict[str, int] = Counter(e.req_id for e in out.excluded)
    for rid, n in sorted(excl_ids.items()):
        words = sorted({e.matched_word for e in out.excluded if e.req_id == rid})
        print(f"    {rid}  x{n}  命中={words}")
    print(f"    剔除需求编号数: {len(excl_ids)} (共 {n_excl} 行)")

    # ---------- 4. 散文块审计 (表格外的需求载体) ----------
    print("\n--- 4. 散文块审计 (表格外的章节内容) ---")
    crossref_words, noreq_words = _prose_words(prof)
    print(
        f"    判别措辞(取自档案): 引用={list(crossref_words) or '(未声明)'} 无要求={list(noreq_words)}"
    )
    prose = []
    for b in blocks:
        if b.section_path not in prefixes and not any(
            b.section_path.startswith(p + ".") for p in prefixes
        ):
            continue
        if b.tables:
            continue
        t = (b.text or "").strip()
        if not t:
            continue
        if noreq_words and any(w in t for w in noreq_words):
            kind = "明示无要求"
        elif crossref_words and any(w in t for w in crossref_words):
            kind = "跨章节引用"
        else:
            kind = "散文需求(需人工判定)"
        prose.append((b.section_path, b.heading, kind, t))
        print(f"    [{kind}] {b.section_path} {b.heading}")
        print(f"        {t[:90]}")
    check(
        "4-散文判别措辞已配置",
        bool(crossref_words) and bool(noreq_words),
        "档案未声明则无法判别, 会漏掉引用类需求",
    )
    check(
        "4-散文块无未判定项",
        not any(k == "散文需求(需人工判定)" for _, _, k, _ in prose),
        f"{len(prose)} 个散文块全部可归类",
    )

    # ---------- 5. 覆盖度结论 ----------
    # 引用穿透产出的需求不在"表格行"账内, 必须单列 —— 否则总数会漏算,
    # 或反过来把引用产出的条目混进表格行对账而破坏恒等式。
    n_ref = 0
    n_crossref = sum(1 for _, _, k, _ in prose if k == "跨章节引用")
    try:
        from aterag.extract import extract_test_conditions

        ex = extract_test_conditions(MODEL, doc_version="B", annotations=load_annotations(MODEL))
        ref_conds = [c for c in ex.conditions if "resolved_reference" in c.flags]
        n_ref = len(ref_conds)
        for c in ref_conds:
            tgt = next((f for f in c.flags if f.startswith("reference_target:")), "")
            print(
                f"    + 引用穿透 {c.req_id} {c.title} [{c.section_path}] {tgt}"
                f" -> {len(c.output_conditions)} 条内容"
            )
    except Exception as e:  # noqa: BLE001
        print(f"    (引用穿透统计不可用: {type(e).__name__}: {e})")

    print("\n--- 5. 覆盖度结论 ---")
    print(f"    表格行 {total_rows}")
    print(f"      ├─ 需求行 {n_req}  ->  非不要求 {n_keep} 条  +  不要求/无要求 {n_excl} 条")
    print(f"      ├─ 属性行 {cls.get('属性行', 0)}  (通信协议基本参数, 非需求条目)")
    print(f"      └─ 参考表 {cls.get('参考表', 0)}  (空开选型参考, 非需求条目)")
    print(f"    散文块 {len(prose)}  ->  " + ", ".join(sorted({k for _, _, k, _ in prose})))
    if n_ref:
        print(f"    引用穿透产出 +{n_ref} 条 (正文为'详见X', 内容在被引用章节)")
    accounted = (
        cls.get("需求行", 0) + cls.get("属性行", 0) + cls.get("参考表", 0) + cls.get("元数据行", 0)
    )
    check("5-所有表格行已归类", accounted == total_rows, f"{accounted}/{total_rows}")
    check(
        "5-无任何行被静默丢弃",
        total_rows == n_req + cls.get("属性行", 0) + cls.get("参考表", 0) + cls.get("元数据行", 0),
        "对账平衡",
    )
    check(
        "5-跨章节引用全部穿透",
        n_ref == n_crossref,
        f"跨章节引用 {n_crossref} 条 -> 穿透产出 {n_ref} 条",
    )

    # ---------- 6. 与 MCP 服务 stats 交叉核对 ----------
    print("\n--- 6. 与服务 stats 交叉核对 ---")
    stats = None
    if args.mcp_stats:
        stats = json.loads(Path(args.mcp_stats).read_text(encoding="utf-8")).get("stats")
    elif args.mcp:
        raw = _fetch_mcp_stats(args.mcp)
        # 服务返回的是 JSON 字符串 (MCP content 约定), 需再解一层
        stats = json.loads(raw).get("stats") if isinstance(raw, str) else (raw or {}).get("stats")
    if stats:
        print(
            f"    MCP stats: {json.dumps({k: stats.get(k) for k in ('rows_total', 'kept', 'excluded', 'unmapped_table_rows')}, ensure_ascii=False)}"
        )
        check(
            "6-MCP rows_total 一致",
            stats.get("rows_total") == n_req,
            f"{stats.get('rows_total')} vs {n_req}",
        )
        check("6-MCP kept 一致", stats.get("kept") == n_keep, f"{stats.get('kept')} vs {n_keep}")
        check(
            "6-MCP excluded 一致",
            stats.get("excluded") == n_excl,
            f"{stats.get('excluded')} vs {n_excl}",
        )
        check(
            "6-MCP 未映射行数=参考表行数",
            stats.get("unmapped_table_rows", 0) == cls.get("参考表", 0),
            f"{stats.get('unmapped_table_rows')} vs {cls.get('参考表', 0)}",
        )
    else:
        print("    (未提供 --mcp/--mcp-stats, 跳过服务侧交叉核对)")

    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n===== {len(results) - n_fail}/{len(results)} passed =====")
    print(
        f"\n答案: 4.3 章节下非'不要求/无要求'的需求 = {n_keep} 条(表格) + {n_ref} 条(引用穿透)"
        f" = {n_keep + n_ref} 条"
    )
    return 0 if n_fail == 0 else 1


def _fetch_mcp_stats(url: str) -> dict | None:
    import urllib.request

    def rpc(method: str, params: dict | None, sid: str | None):
        payload: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            payload["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if sid:
            headers["Mcp-Session-Id"] = sid
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=300) as r:  # noqa: S310 固定内网地址
            return r.read().decode("utf-8", "replace"), r.headers.get("Mcp-Session-Id")

    try:
        _, sid = rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "coverage-audit", "version": "1"},
            },
            None,
        )
        body, _ = rpc(
            "tools/call", {"name": "extract_test_conditions", "arguments": {"model_id": MODEL}}, sid
        )
        for line in body.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])["result"]["content"][0]["text"]
    except Exception as e:  # noqa: BLE001
        print(f"    MCP 不可达: {type(e).__name__}: {e}")
    return None


if __name__ == "__main__":
    sys.exit(main())
