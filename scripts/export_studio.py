"""bundle 导出 CLI (缝⑥ 出口) —— 把抽取结果导出为 ATEStudio 可导入的 bundle。

产物落盘而不直接推 HTTP, 理由与"评审走 git"同源:
  * bundle 是审计凭据, 必须进版本库, 变更可 diff、可回溯;
  * 先落盘再推送, 推送失败时产物仍在, 不用重跑抽取。

基线文件 (--baseline): 记录上一次导出的需求编号, 用于计算本次"消失的需求"
(规格书改版后被删掉的条目)。只存编号不存全文 —— 基线的作用是回答"少了什么",
不是当数据库用。基线默认不进版本库(它每次导出都变, 会淹没真实 diff)。

用法:
  .venv\\Scripts\\python.exe scripts/export_studio.py --model PA601-D54A
  .venv\\Scripts\\python.exe scripts/export_studio.py -m PA601-D54A --print-fingerprint
  .venv\\Scripts\\python.exe scripts/export_studio.py -m PA601-D54A --out dist/bundle.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from aterag.config import get_settings  # noqa: E402
from aterag.extract import (  # noqa: E402
    PatternBook,
    ProfileBook,
    extract_test_conditions,
    load_annotations,
)
from aterag.extract.bundle import (  # noqa: E402
    BUNDLE_VERSION,
    BundleModel,
    bundle_from_result,
    contract_hash,
)

DEFAULT_BASELINE = Path("rag_storage/last_bundle_baseline.json")
DEFAULT_OUT = Path("rag_storage/exports/studio_bundle.json")

#: 与 extract.api.DEFAULT_BLOCKS_DIR 保持一致: 离线 blocks 重跑, 不查库。
#: 板卡 postgres 是部署镜像, 导出以本地抽取为准, 避免两份数据漂移。
DEFAULT_BLOCKS_DIR = "rag_storage/blocks"


def _load_baseline(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 基线损坏不该阻断导出 —— 首次导出或基线被误删时都应能继续。
        return None


def _save_baseline(path: Path, bundle: BundleModel) -> None:
    """基线只存规格书原编号, 不存消歧后的 requirement_code。

    原因: requirement_code 带行限定词, 判据一变它就变。拿它做"消失检测"
    会把"限值被改"误判成"旧需求全删了 + 新需求全加了", 下游据此把用例
    标 stale 是错的。规格书原编号才是稳定的身份标识。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "product_code": bundle.product_code,
                "doc_version": bundle.doc_version,
                "product_doc_fingerprint": bundle.product_doc_fingerprint,
                "bundle_fingerprint": bundle.bundle_fingerprint,
                "spec_requirement_ids": sorted(
                    {r.spec_requirement_id or r.requirement_code for r in bundle.requirements}
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 ATEStudio bundle")
    ap.add_argument("-m", "--model", required=True, help="型号 id, 如 PA601-D54A")
    ap.add_argument("--doc-version", default="B")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--blocks-dir", type=Path, default=DEFAULT_BLOCKS_DIR)
    ap.add_argument(
        "--print-fingerprint",
        action="store_true",
        help="只打印契约与 bundle 指纹, 不落盘 (供两仓对账)",
    )
    ap.add_argument("--brief", action="store_true", help="人类可读摘要")
    args = ap.parse_args()

    settings = get_settings()
    prof = ProfileBook.load(settings.doc_profiles_path)
    book = PatternBook.load(settings.condition_patterns_path)
    ann_dir = Path(settings.annotations_dir)
    ann = load_annotations(str(ann_dir / f"{args.model}.conditions.yaml"))

    result = extract_test_conditions(
        args.model,
        doc_version=args.doc_version,
        profiles=prof,
        patterns=book,
        annotations=ann,
        blocks_dir=args.blocks_dir,
    )

    baseline = _load_baseline(args.baseline)
    # 消失需求由 bundle_from_result 依基线自动算出, 不在调用侧重复计算
    bundle = bundle_from_result(
        result,
        product_doc_fingerprint=result.stats.get("product_doc_fingerprint", ""),
        baseline=baseline,
    )

    if args.print_fingerprint:
        print(f"contract_hash  = {contract_hash()}")
        print(f"bundle_version = {BUNDLE_VERSION}")
        print(f"bundle_fp      = {bundle.bundle_fingerprint}")
        print(f"product_doc_fp = {bundle.product_doc_fingerprint or '(未提供)'}")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        bundle.model_dump_json(indent=2, exclude_none=False) + "\n", encoding="utf-8"
    )
    _save_baseline(args.baseline, bundle)

    n_req = len(bundle.requirements)
    n_scen = sum(len(r.scenarios) for r in bundle.requirements)
    n_cond = sum(len(r.input_conditions) + len(r.output_conditions) for r in bundle.requirements)
    draft = sum(
        1
        for r in bundle.requirements
        for c in (*r.input_conditions, *r.output_conditions)
        if c.status == "draft"
    )
    print(f"[OK] bundle -> {args.out}")
    print(f"     契约 {BUNDLE_VERSION} hash={bundle.contract_hash}")
    print(f"     需求 {n_req} | 场景 {n_scen} | 条件子句 {n_cond} (待审 {draft})")
    print(f"     剔除 {len(bundle.excluded)} | 待审 {len(bundle.review)}")
    print(f"     消失需求 {len(bundle.removed_requirement_codes) or 0}")
    print(f"     bundle 指纹 {bundle.bundle_fingerprint}")
    print(f"     基线 -> {args.baseline}")

    if args.brief:
        for r in bundle.requirements[:10]:
            print(f"     - {r.requirement_code} {r.title} | 场景 {len(r.scenarios)}")
    if draft:
        print(f"[!] 含 {draft} 条待审子句: 下游按 status=draft 展示, 未批准不得挂执行序列")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
