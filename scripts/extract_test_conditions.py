"""产测条件抽取 CLI (缝③ 出口之一).

把规格书 4.3 章节里的产测条件抽成"输入条件(外部状态) -> 输出条件(自身信号状态)"对,
并把剔除项与待审项一并输出 —— 三桶都可见, 剔除行为本身可审计。

默认输出 JSON 到 stdout (可 --out 落盘, 便于 git diff 评审), --brief 给人类可读摘要。

用法:
  .venv\\Scripts\\python.exe scripts/extract_test_conditions.py --model PA601-D54A
  .venv\\Scripts\\python.exe scripts/extract_test_conditions.py -m PA601-D54A --brief
  .venv\\Scripts\\python.exe scripts/extract_test_conditions.py -m PA601-D54A --source postgres
  .venv\\Scripts\\python.exe scripts/extract_test_conditions.py -m PA601-D54A \\
      --print-fingerprint SR-PA601-D54A-1213
  .venv\\Scripts\\python.exe scripts/extract_test_conditions.py --profiles
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings  # noqa: E402
from aterag.extract import (  # noqa: E402
    PatternBook,
    ProfileBook,
    extract_test_conditions,
    load_annotations,
    row_fingerprint,
)
from aterag.extract.api import (  # noqa: E402
    SRC_BLOCKS,
    SRC_POSTGRES,
    load_blocks,
    rows_from_blocks,
)
from aterag.extract.selector import select_sections  # noqa: E402

MAX_TEXT = 200


def brief(result) -> None:
    sel = result.selection
    print(f"型号 {result.model_id} (v{result.doc_version or '?'})  profile={result.profile}")
    if sel:
        print(f"章节: {sel.matched_headings} -> {sel.section_prefixes}")
        print(f"块: {sel.blocks_selected}/{sel.blocks_total}")
    print()
    print("=== 统计 ===")
    for k, v in result.stats.items():
        print(f"  {k}: {v}")
    print()
    print(f"=== 条件 (前 10 / 共 {len(result.conditions)}) ===")
    for c in result.conditions[:10]:
        lim = ""
        if c.limits:
            lim = " 限值=" + json.dumps(c.limits, ensure_ascii=False)
        print(f"  [{c.req_id}] {c.title}  (role={c.role}{lim})")
        for i in c.input_conditions:
            print(f"      激励 {i.kind}: {i.text[:MAX_TEXT]}")
        for o in c.output_conditions:
            print(f"      响应 {o.kind}: {o.text[:MAX_TEXT]}")
        if c.flags:
            print(f"      flags: {c.flags}")
    print()
    print(f"=== 剔除 (前 10 / 共 {len(result.excluded)}) ===")
    for e in result.excluded[:10]:
        print(f"  [{e.req_id}] {e.title[:30]} :: {e.reason}")
    if result.needs_review:
        print()
        print(f"=== 待审 (共 {len(result.needs_review)}) ===")
        for r in result.needs_review[:10]:
            print(f"  [{r.kind}] {r.section_path} {r.detail[:MAX_TEXT]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="抽取产测输入/输出条件")
    ap.add_argument("-m", "--model", help="型号 ID (默认读 registry 里的第一个)")
    ap.add_argument("--profile", help="文档档案名 (默认用档案的 default_profile)")
    ap.add_argument(
        "--source",
        choices=[SRC_BLOCKS, SRC_POSTGRES],
        default=SRC_BLOCKS,
        help="数据来源: blocks=离线确定性(默认) / postgres=RAG 实际落库",
    )
    ap.add_argument("--section-keyword", help="覆盖档案的章节关键字")
    ap.add_argument("--out", help="JSON 输出路径 (默认 stdout)")
    ap.add_argument("--brief", action="store_true", help="人类可读摘要而非 JSON")
    ap.add_argument("--profiles", action="store_true", help="列出全部文档档案后退出")
    ap.add_argument("--stats-only", action="store_true", help="只输出统计")
    ap.add_argument("--print-fingerprint", metavar="REQ_ID", help="打印某行的指纹 (写注记用)")
    args = ap.parse_args()

    settings = get_settings()
    profiles = ProfileBook.load()
    if args.profiles:
        print(f"档案: {profiles.source_path} | 默认: {profiles.default_profile}\n")
        for name, p in profiles.profiles.items():
            print(f"  {name}: {p.description}")
            print(f"    章节关键字: {list(p.section_keywords)}")
            print(f"    剔除词: {list(p.exclude_words)}")
            print(f"    先验: { {k: (v.role, v.limits_to) for k, v in p.section_priors.items()} }")
        return 0

    model_id = args.model
    if not model_id:
        from aterag.registry import Registry

        model_id = sorted(Registry.load(settings).products)[0]
    doc_version = ""
    try:
        from aterag.registry import Registry

        entry = Registry.load(settings).products.get(model_id)
        doc_version = entry.doc_version if entry else ""
    except Exception:  # noqa: BLE001  注册表读不到不阻断抽取
        pass

    # 打印指纹: 供人工写注记时填 fingerprint 字段
    if args.print_fingerprint:
        prof = profiles.get(args.profile)
        blocks = load_blocks(model_id)
        sel = select_sections(blocks, prof.section_keywords)
        rows, _, _ = rows_from_blocks(blocks, model_id, doc_version, sel.section_prefixes)
        for r in rows:
            if r["req_id"] == args.print_fingerprint:
                print(f"{r['req_id']}  fingerprint={row_fingerprint(r)}")
                print(
                    f"  title={r['title']}  section={r['section_path']}  priority={r['priority']}"
                )
                print(f"  notes={str(r['notes'])[:300]}")
                return 0
        print(f"未找到 {args.print_fingerprint}")
        return 1

    if args.section_keyword:
        # 临时覆盖关键字: 复制档案并改写, 不落盘
        base = profiles.get(args.profile)
        profiles.profiles[base.name] = type(base)(
            name=base.name,
            section_keywords=(args.section_keyword,),
            exclude_words=base.exclude_words,
            include_prose=base.include_prose,
            section_priors=base.section_priors,
            default_role=base.default_role,
            default_limits_to=base.default_limits_to,
            description=base.description,
        )

    result = extract_test_conditions(
        model_id,
        doc_version=doc_version,
        profile_name=args.profile,
        profiles=profiles,
        patterns=PatternBook.load(),
        annotations=load_annotations(model_id),
        source=args.source,
        dsn=settings.postgres_dsn,
    )

    if args.brief:
        brief(result)
    elif args.stats_only:
        print(json.dumps(result.stats, ensure_ascii=False, indent=2))
    else:
        out = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(out, encoding="utf-8")
            print(
                f"已写入 {args.out} ({len(result.conditions)} 条条件, {len(result.excluded)} 条剔除)"
            )
        else:
            print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
