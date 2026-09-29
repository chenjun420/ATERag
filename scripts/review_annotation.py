"""人工注记评审工具 (把"怎么评审确认"变成一条命令, 而不是"谁有空手工改 YAML").

三种模式:
  --list    列出全部注记及其状态 (draft / approved)
  --show     并排显示: 规格书原文 -> 规则推断结果 -> 注记草案, 供逐项核对
  --approve  签字: 置 status=approved 并写 approved_by / approved_at
  --check    CI 门禁: 存在未评审注记则退出码 1

评审要点 (--show 会把这几项直接算好, 不用评审人自己比对):
  * fingerprint 是否与当前行一致 —— 不一致说明规格书已改版, 注记已自动失效, 须重写
  * 注记每个子句的 text 是否能在规格书原文里找到 (防止编造)
  * value 与原文是否一致 (人工判断, 但原文片段已高亮给出)
  * kind 是否都在封闭词表内
  * 注记相对规则推断是补充还是改写 (改写需说明理由)

用法:
  .venv\\Scripts\\python.exe scripts/review_annotation.py --list
  .venv\\Scripts\\python.exe scripts/review_annotation.py -m PA601-D54A --show SR-PA601-D54A-1213
  .venv\\Scripts\\python.exe scripts/review_annotation.py -m PA601-D54A --approve SR-PA601-D54A-1213 --by 张三
  .venv\\Scripts\\python.exe scripts/review_annotation.py -m PA601-D54A --check
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

from aterag.config import get_settings  # noqa: E402
from aterag.extract import (  # noqa: E402
    AnnotationBook,
    PatternBook,
    ProfileBook,
    apply_sieve,
    load_blocks,
    row_fingerprint,
    rows_from_blocks,
    select_sections,
)
from aterag.extract.assembler import assemble  # noqa: E402

PASS, FAIL = "✅", "❌"


def _doc_text(model_id: str) -> str:
    """规格书原文 (从已入库侧车对应的 md 读, 便于核对子句是否有原文支撑)。"""
    for cand in (
        f"{model_id} 定制电源技术规格书.md",
        f"{model_id}.md",
    ):
        p = Path(cand)
        if p.exists():
            return p.read_text(encoding="utf-8")
    specs = sorted(Path("specs").glob(f"{model_id}*.md")) if Path("specs").is_dir() else []
    if specs:
        return specs[0].read_text(encoding="utf-8")
    return ""


def cmd_list(model_id: str, ann: AnnotationBook) -> int:
    print(f"=== 注记清单 ({ann.source_path}) ===")
    if not ann.entries:
        print("  (无注记)")
        return 0
    by = ann.by_status()
    for status, ids in sorted(by.items()):
        mark = FAIL if status != "approved" else PASS
        print(f"  {mark} {status}: {len(ids)} 条")
        for rid in ids:
            e = ann.get(rid)
            assert e
            print(
                f"      {rid}  fingerprint={e.fingerprint}  reason={e.data.get('reason', '')[:50]}"
            )
            if status == "approved":
                print(f"          签字: {e.data.get('approved_by')} @ {e.data.get('approved_at')}")
    draft = by.get("draft", [])
    if draft:
        print(f"\n  待评审 {len(draft)} 条: {' '.join(draft)}")
        print("  评审: --show <REQ_ID> 逐项核对, 然后 --approve <REQ_ID> --by <评审人>")
    return 0


def cmd_show(model_id: str, req_id: str, ann: AnnotationBook) -> int:
    entry = ann.get(req_id)
    if entry is None:
        print(f"[FAIL] 未找到注记: {req_id}")
        return 1

    prof = ProfileBook.load().get(None)
    blocks = load_blocks(model_id)
    sel = select_sections(blocks, prof.section_keywords)
    rows, _, _ = rows_from_blocks(blocks, model_id, "", sel.section_prefixes)
    row = next((r for r in rows if r["req_id"] == req_id), None)
    if row is None:
        print(f"[FAIL] {req_id} 不在章节 {sel.section_prefixes} 的抽取范围内")
        return 1

    # 该行若被剔除, 评审无意义 (注记不该把"不要求"的需求救回来)
    if not apply_sieve([row], prof.exclude_words).kept:
        print(f"[FAIL] {req_id} 等级为剔除词, 属被排除条目, 不接受人工注记")
        return 1

    print(f"=== {req_id} 评审单 ===")
    print(f"章节: {row['section_path']}  等级: {row['priority']}  单位: {row['unit']}")
    print(f"限值: min={row['min']} typ={row['typ']} max={row['max']}")
    print(f"备注原文: {str(row['notes'])[:300]}")
    print()

    fp_now = row_fingerprint(row)
    fresh = entry.fingerprint == fp_now
    print(f"1) 指纹  {'一致' if fresh else '不一致 —— 规格书已改版, 注记已自动失效, 须重写'}")
    print(f"   注记 {entry.fingerprint} / 当前 {fp_now}")
    print(f"2) 状态  {entry.status}  (approved_by={entry.data.get('approved_by')})")
    print()

    book = PatternBook.load()
    prior = prof.prior_for(str(row.get("section_path", "")))
    asm = assemble(row, role=prior.role, limits_to=prior.limits_to, book=book, annotations=None)
    print("3) 规则推断结果 (未加注记时系统会产出的内容):")
    if not asm.inputs and not asm.outputs:
        print("   (无 —— 这正是需要人工注记的原因)")
    for i in asm.inputs:
        print(f"   激励 {i.kind}: {i.text[:70]}")
    for o in asm.outputs:
        print(f"   响应 {o.kind}: {o.text[:70]}")
    print()

    doc = _doc_text(model_id)
    print("4) 注记草案逐项核对 (text 必须能在规格书原文找到):")
    ok_all = True
    for role in ("input", "output"):
        for k, spec in enumerate(entry.data.get(role) or [], 1):
            text = str(spec.get("text", ""))
            kind = str(spec.get("kind", ""))
            in_doc = (not doc) or (text and text.split("(")[0].strip()[:12] in doc)
            kind_ok = kind in book.kinds
            mark = PASS if (in_doc and kind_ok) else FAIL
            ok_all = ok_all and in_doc and kind_ok
            print(f"   {mark} {role}[{k}] kind={kind} 原文可溯={in_doc} 词表内={kind_ok}")
            print(f"        text : {text}")
            if spec.get("value"):
                print(f"        value: {spec['value']}")
    print()
    print(f"5) 理由: {entry.data.get('reason', '(未填写)')}")
    print()
    verdict = "可签字" if (fresh and ok_all) else "暂不可签字 (见上方 ❌)"
    print(f"评审结论: {verdict}")
    if fresh and ok_all and entry.status != "approved":
        print(
            f"签字命令: scripts/review_annotation.py -m {model_id} --approve {req_id} --by <评审人>"
        )
    return 0 if (fresh and ok_all) else 1


def cmd_approve(model_id: str, req_id: str, by: str, ann: AnnotationBook) -> int:
    p = Path(ann.source_path)
    if not p.exists():
        print(f"[FAIL] 注记文件不存在: {p}")
        return 1
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    entries = doc.get("entries") or {}
    if req_id not in entries:
        print(f"[FAIL] 未找到注记: {req_id}")
        return 1

    # 签字前强制重跑核对: 防止过期或子句无原文支撑时签字落空
    if cmd_show(model_id, req_id, ann) != 0:
        print("\n[FAIL] 核对未通过, 拒绝签字")
        return 1

    entries[req_id]["status"] = "approved"
    entries[req_id]["approved_by"] = by
    entries[req_id]["approved_at"] = _dt.date.today().isoformat()
    doc["entries"] = entries
    p.write_text(
        "# 人工语义注记: 语义更准但规则切不出的需求, 按内容指纹自动失效。\n"
        "# status=draft 仍生效但置信度 proposed 且 CI --check 会红; approved 为已签字。\n"
        "# 评审: scripts/review_annotation.py -m <model> --show <REQ_ID>\n"
        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"\n[OK] {req_id} 已签字: {by} @ {_dt.date.today().isoformat()}")
    print("     置信度由 proposed 升为 annotated; 请提交该文件让 CI 复核")
    return 0


def cmd_check(model_id: str, ann: AnnotationBook) -> int:
    by = ann.by_status()
    draft = by.get("draft", [])
    approved = by.get("approved", [])
    if draft:
        print(f"{FAIL} 存在未评审注记 {len(draft)} 条: {' '.join(draft)}")
        print("   未评审注记虽已生效, 但语义未经签字, 不得视为已确认:")
        print("     - 下游应按 confidence=proposed 过滤 (见 stats.annotation_draft)")
        print(f"   评审: scripts/review_annotation.py -m {model_id} --show <REQ_ID>")
        return 1
    print(f"{PASS} 全部注记已评审 (approved {len(approved)} 条)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="人工注记评审")
    ap.add_argument("-m", "--model", default="PA601-D54A", help="型号 ID")
    ap.add_argument("--list", action="store_true", help="列出全部注记")
    ap.add_argument("--show", metavar="REQ_ID", help="并排显示原文/规则结果/注记草案")
    ap.add_argument("--approve", metavar="REQ_ID", help="签字通过")
    ap.add_argument("--by", default="", help="评审人 (配合 --approve)")
    ap.add_argument("--check", action="store_true", help="CI 门禁: 有未评审注记则失败")
    args = ap.parse_args()

    ann_dir = get_settings().annotations_dir
    ann = AnnotationBook.load(Path(ann_dir) / f"{args.model}.conditions.yaml")

    if args.check:
        return cmd_check(args.model, ann)
    if args.approve:
        if not args.by:
            print("[FAIL] --approve 必须同时给 --by <评审人> (签字要留痕)")
            return 1
        return cmd_approve(args.model, args.approve, args.by, ann)
    if args.show:
        return cmd_show(args.model, args.show, ann)
    return cmd_list(args.model, ann)


if __name__ == "__main__":
    sys.exit(main())
