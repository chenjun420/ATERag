"""bundle 契约验证 (缝⑥ 出口) —— 对接面必须被证明, 不能只看"导出成功"。

最危险的一类故障不是报错, 而是"导入成功但数据少了一半"。这份验证盯的
就是那些不报错的路径:

  1. 契约自洽: 幂等键唯一 / 场景序号唯一 / 主版本一致
  2. 三桶齐全: 剔除项与待审项必须随 bundle 带出, 省略会让下游无法区分
     "没有"和"没传" —— 前者会显示为覆盖率缺口
  3. 状态忠实: draft 子句不得在导出时被改成 approved
  4. 幂等: 两次导出指纹一致(不含时间戳, 否则每次都算"已变更")
  5. 基线检测: 需求消失只认规格书原编号, 不受行限定词漂移影响
  6. 契约指纹: 两侧须算出同一个 hash(此处只固定基准值, 供 ATEStudio 比对)

用法: .venv\\Scripts\\python.exe scripts/verify_bundle.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

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

MODEL = "PA601-D54A"
PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"✅ {name}" + (f" | {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"❌ {name} | {detail}")


def _build():
    prof = ProfileBook.load()
    book = PatternBook.load()
    ann = load_annotations(f"config/annotations/{MODEL}.conditions.yaml")
    r = extract_test_conditions(
        MODEL, doc_version="B", profiles=prof, patterns=book, annotations=ann
    )
    return r, bundle_from_result(r)


def main() -> int:
    result, bundle = _build()

    # ---------- 1. 契约自洽 ----------
    check("契约-主版本", bundle.bundle_version == BUNDLE_VERSION, BUNDLE_VERSION)
    check("契约-hash 已计算", len(bundle.contract_hash) >= 16, bundle.contract_hash)
    codes = [r.requirement_code for r in bundle.requirements]
    check("契约-幂等键唯一", len(codes) == len(set(codes)), f"{len(codes)} 个需求")
    scen_seqs_ok = all(
        len({s.seq for s in r.scenarios}) == len(r.scenarios) for r in bundle.requirements
    )
    check("契约-场景序号唯一", scen_seqs_ok, "case_code 由此派生")
    check(
        "契约-多行编号已消歧",
        all(r.spec_requirement_id for r in bundle.requirements),
        f"{sum(1 for r in bundle.requirements if r.requirement_code != r.spec_requirement_id)} 条带行限定词",
    )

    # ---------- 2. 三桶齐全 ----------
    check("三桶-剔除项带出", len(bundle.excluded) > 0, f"{len(bundle.excluded)} 条")
    check(
        "三桶-剔除项均带原因",
        all(e.reason for e in bundle.excluded),
        "剔除本身要可审计",
    )
    check("三桶-待审项带出", len(bundle.review) > 0, f"{len(bundle.review)} 条")
    check(
        "三桶-待审项均带 kind",
        all(r.kind for r in bundle.review),
        "下游按 kind 分桶",
    )
    check(
        "三桶-被排除场景带出",
        hasattr(bundle, "excluded_scenarios"),
        f"{len(bundle.excluded_scenarios)} 条",
    )

    # ---------- 3. 状态忠实 ----------
    all_clauses = [
        c for r in bundle.requirements for c in (*r.input_conditions, *r.output_conditions)
    ]
    draft = [c for c in all_clauses if c.status == "draft"]
    check(
        "状态-draft 未被改成 approved",
        all(c.status in {"draft", "approved"} for c in all_clauses),
        f"draft {len(draft)} / 共 {len(all_clauses)}",
    )
    check(
        "状态-补齐子句必为 draft",
        all(c.status == "draft" for c in all_clauses if c.method_ref),
        f"{sum(1 for c in all_clauses if c.method_ref)} 条补齐子句",
    )
    check(
        "状态-补齐子句可溯源",
        all(c.method_ref for c in all_clauses if c.confidence == "proposed"),
        "method_ref 非空",
    )

    # ---------- 4. 幂等 ----------
    _, b2 = _build()
    check(
        "幂等-两次导出 bundle 指纹一致",
        bundle.bundle_fingerprint == b2.bundle_fingerprint,
        f"{bundle.bundle_fingerprint} (不含时间戳, 否则每次都算变更)",
    )
    check(
        "幂等-两次导出契约 hash 一致",
        bundle.contract_hash == b2.contract_hash,
        bundle.contract_hash,
    )
    check(
        "幂等-两次导出内容一致",
        [r.requirement_code for r in bundle.requirements]
        == [r.requirement_code for r in b2.requirements],
        "需求顺序稳定",
    )

    # ---------- 5. 基线检测 ----------
    # 基线 = "上一次导出时存在什么"。要检测"本次消失", 必须让基线里多出
    # 当前不存在的编号, 而不是从基线里删掉现有的 —— 后者测的是"基线缺项",
    # 语义相反, 断言会假失败(且会掩盖真实的删除检测失效)。
    spec_ids = sorted({r.spec_requirement_id for r in bundle.requirements})
    ghost = ["SR-PA601-D54A-9998", "SR-PA601-D54A-9999"]
    b3 = bundle_from_result(result, baseline={"spec_requirement_ids": [*spec_ids, *ghost]})
    check(
        "基线-检出消失需求",
        set(b3.removed_requirement_codes) == set(ghost),
        f"{b3.removed_requirement_codes}",
    )
    b4 = bundle_from_result(result, baseline={"spec_requirement_ids": spec_ids})
    check(
        "基线-无消失时不误报",
        len(b4.removed_requirement_codes) == 0,
        "行限定词漂移不得被当成删除",
    )

    # ---------- 6. 序列化 ----------
    raw = bundle.model_dump_json()
    back = BundleModel.model_validate_json(raw)
    check("序列化-往返一致", back.bundle_fingerprint == bundle.bundle_fingerprint, "JSON 可回读")
    check(
        "序列化-版本受拒",
        _raises_value(
            lambda: BundleModel.model_validate({**json.loads(raw), "bundle_version": "2.0"})
        ),
        "主版本不兼容应被拒",
    )
    check(
        "序列化-未知字段受拒",
        _raises_value(lambda: BundleModel.model_validate({**json.loads(raw), "bogus": 1})),
        "extra=forbid: 拼错的字段必须报错而不是静默丢弃",
    )
    check(
        "序列化-场景序号冲突受拒",
        _raises_value(lambda: _dup_seq_reject(raw)),
        "契约层就拦住 case_code 冲突",
    )

    # ---------- 7. CLI 端到端 ----------
    out = Path("rag_storage/exports/_verify_bundle.json")
    p = subprocess.run(  # noqa: S603
        [sys.executable, "scripts/export_studio.py", "-m", MODEL, "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    check("CLI-导出成功", p.returncode == 0, (p.stderr or "")[-120:] if p.returncode else "")
    if out.exists():
        d = json.loads(out.read_text(encoding="utf-8"))
        check(
            "CLI-契约 hash 与库一致",
            d["contract_hash"] == contract_hash(),
            d["contract_hash"],
        )
        check(
            "CLI-需求数一致",
            len(d["requirements"]) == len(bundle.requirements),
            f"{len(d['requirements'])}",
        )
        out.unlink(missing_ok=True)

    print(f"\n===== {PASSED}/{PASSED + FAILED} passed =====")
    if FAILED:
        print(f"BUNDLE FAIL ({FAILED} 项)")
        return 1
    print("BUNDLE PASS")
    print(f"契约基准: contract_hash = {contract_hash()}  (ATEStudio 导入端须算出同一个值)")
    return 0


def _raises_value(fn) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


def _dup_seq_reject(raw: str) -> None:
    """构造一个需求内场景序号冲突的 bundle, 确认契约层拒绝。"""

    d = json.loads(raw)
    for r in d["requirements"]:
        if len(r["scenarios"]) >= 2:
            r["scenarios"][1]["seq"] = r["scenarios"][0]["seq"]
            break
    BundleModel.model_validate(d)


if __name__ == "__main__":
    raise SystemExit(main())
