"""方法库签字工具 —— 把「业界方法等签字」变成可执行动作。

背景
----
补齐层产出的子句一律 draft/proposed, 未人审不得生效(红线: 未批准不生效)。
54/95 条需求行含这类草案子句, 但此前**没有签署通道**: ``--approve`` 只服务
人工注记(逐条), 方法库是配置级知识 —— 签的是「这个方法成立吗」, 一次签字
覆盖它命中的全部行。逐行签 54 次既不可行, 也把评审人变成了盖章机器。

签字绑定**方法内容指纹** (sha256 前 16 位): test_methods.yaml 里该方法任何
改动都让签字自动失效, 必须重新评审 —— 与注记的指纹纪律同构。

模式
----
  --list     列出全部方法与签字状态 (signed / unsigned / stale)
  --show     显示一个方法的完整内容 + 依据 + 命中行数, 供逐项核对
  --approve  签字: 写 data/annotations/method_signoffs.yaml (记录签字人/日期)
  --check    CI 门禁: 存在未签方法则退出码 1

评审要点 (--show 给出):
  * basis 是否有可查的出处 (标准号/行业实践)
  * conditions 的 kind 是否在封闭词表内、note 是否可执行
  * 方法命中的需求行数与样例 —— 签字影响范围可见
  * 相对规格书原文是"补缺侧"还是"覆盖"? (本层只补不改, 越权即拒绝)

用法:
  .venv\\Scripts\\python.exe scripts/review_method.py --list
  .venv\\Scripts\\python.exe scripts/review_method.py --show psu_output_tempco
  .venv\\Scripts\\python.exe scripts/review_method.py --approve psu_output_tempco --by 张三
  .venv\\Scripts\\python.exe scripts/review_method.py --check
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

from aterag.extract.supplement import (  # noqa: E402
    DEFAULT_METHODS_PATH,
    DEFAULT_SIGNOFFS_PATH,
    load_signoffs,
    method_fingerprint,
)

PASS, FAIL, WARN = "✅", "❌", "⚠️"


def _methods_raw(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(doc.get("methods") or [])


def _signoff_state(signoff_path: Path, methods_raw: list[dict]) -> dict[str, str]:
    """方法 id -> signed / stale / unsigned。"""
    signoffs = load_signoffs(signoff_path)
    out: dict[str, str] = {}
    for m in methods_raw:
        mid = str(m.get("id", ""))
        sign = signoffs.get(mid) or {}
        signed_fp = str(sign.get("fingerprint", ""))
        actual_fp = method_fingerprint(m)
        if not signed_fp:
            out[mid] = "unsigned"
        elif signed_fp == actual_fp:
            out[mid] = "signed"
        else:
            out[mid] = "stale"
    return out


def cmd_list(signoff_path: Path) -> int:
    raw = _methods_raw(DEFAULT_METHODS_PATH)
    states = _signoff_state(signoff_path, raw)
    signoffs = load_signoffs(signoff_path)
    from collections import Counter

    dist = Counter(states.values())
    print(f"=== 方法签字清单 ({DEFAULT_METHODS_PATH}) ===")
    print(
        f"  共 {len(raw)} 条: signed={dist['signed']} unsigned={dist['unsigned']} stale={dist['stale']}"
    )
    for m in raw:
        mid = str(m.get("id", ""))
        st = states[mid]
        mark = {"signed": PASS, "unsigned": WARN, "stale": FAIL}[st]
        extra = ""
        if st == "signed":
            s = signoffs[mid]
            extra = f"  签字: {s.get('approved_by')} @ {s.get('approved_at')}"
        elif st == "stale":
            extra = "  签字已过期(方法内容改过) — 需重新评审"
        print(f"  {mark} {mid}{extra}")
    if dist["unsigned"] or dist["stale"]:
        print("\n  签字: --show <method_id> 核对, 然后 --approve <method_id> --by <评审人>")
    return 0


def cmd_show(method_id: str, signoff_path: Path) -> int:
    raw = _methods_raw(DEFAULT_METHODS_PATH)
    entry = next((m for m in raw if str(m.get("id", "")) == method_id), None)
    if entry is None:
        print(f"[FAIL] 方法不存在: {method_id}")
        return 1
    states = _signoff_state(signoff_path, raw)
    st = states[method_id]
    print(f"=== 方法评审: {method_id} ===")
    print(f"1) 状态: {st}")
    if st == "stale":
        s = load_signoffs(signoff_path).get(method_id) or {}
        print(
            f"   原签字: {s.get('approved_by')} @ {s.get('approved_at')} "
            f"(指纹 {s.get('fingerprint')} -> 现 {method_fingerprint(entry)})"
        )
    print(f"2) 指纹: {method_fingerprint(entry)}")
    print(f"3) 依据: {entry.get('basis', '')}")
    print(
        f"4) verdict={entry.get('verdict')} supplies={entry.get('supplies')} "
        f"applies={entry.get('applies', 'on_missing_side')}"
    )
    ap = entry.get("applies_to") or {}
    print(f"5) 适用: {ap}")
    print("6) 待补条件:")
    for c in entry.get("conditions") or []:
        val = f" value={c.get('value')}" if c.get("value") else ""
        print(f"     - kind={c.get('kind')}{val}")
        note = c.get("note") or c.get("text")
        if note:
            print(f"       note: {note}")
    hints = entry.get("measurement_hints") or {}
    if hints:
        print("7) 测量提示:")
        for k, v in hints.items():
            print(f"     {k}: {v}")
    kref = entry.get("knowledge_ref") or []
    if kref:
        print(f"8) 知识引用: {kref}")
    print()
    print("评审结论: 核对依据与条件可执行后, 运行 --approve 签字")
    return 0


def cmd_approve(method_id: str, by: str, signoff_path: Path) -> int:
    if not by.strip():
        print("[FAIL] --approve 必须同时给 --by <评审人> (签字要留痕)")
        return 1
    if not method_id:
        print("[FAIL] --approve 必须给方法 id")
        return 1
    raw = _methods_raw(DEFAULT_METHODS_PATH)
    entry = next((m for m in raw if str(m.get("id", "")) == method_id), None)
    if entry is None:
        print(f"[FAIL] 方法不存在: {method_id}")
        return 1
    fp = method_fingerprint(entry)
    doc = {}
    header = (
        "# 方法签字书: 业界方法库 (config/test_methods.yaml) 的人工评审记录。\n"
        "# 指纹绑定方法内容 —— 方法任何改动都会让签字自动失效(stale), 需重新评审。\n"
        "# 写入: scripts/review_method.py --approve <method_id> --by <评审人>\n"
    )
    if signoff_path.exists():
        doc = yaml.safe_load(signoff_path.read_text(encoding="utf-8")) or {}
    methods = doc.get("methods") or {}
    prev = methods.get(method_id) or {}
    if prev.get("fingerprint") == fp:
        print(f"[FAIL] {method_id} 已处于当前指纹的签字状态, 无需重复批准")
        return 1
    methods[method_id] = {
        "fingerprint": fp,
        "approved_by": by.strip(),
        "approved_at": _dt.date.today().isoformat(),
        "reason": prev.get("reason") or "业界方法评审通过",
    }
    doc["methods"] = methods
    signoff_path.parent.mkdir(parents=True, exist_ok=True)
    signoff_path.write_text(
        header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"[OK] {method_id} 已签字: {by} @ {_dt.date.today().isoformat()} (指纹 {fp})")
    print("     重新运行抽取/落库后, 该方法产出的子句 status -> approved")
    print("     请提交 method_signoffs.yaml 让 CI 复核")
    return 0


def cmd_check(signoff_path: Path) -> int:
    raw = _methods_raw(DEFAULT_METHODS_PATH)
    states = _signoff_state(signoff_path, raw)
    unsigned = [k for k, v in states.items() if v == "unsigned"]
    stale = [k for k, v in states.items() if v == "stale"]
    if unsigned or stale:
        if unsigned:
            print(f"{FAIL} 存在未签字方法 {len(unsigned)}: {' '.join(unsigned)}")
        if stale:
            print(f"{FAIL} 存在签字过期方法 {len(stale)}: {' '.join(stale)}")
        print("   未签字的方法其子句为 draft, 不得视为已批准产测条件:")
        print("     - 落库侧保持 PENDING, 不冒充 COVERED")
        print("   评审: --show <method_id> 逐项核对, 然后 --approve <method_id> --by <评审人>")
        return 1
    print(f"{PASS} 全部 {len(raw)} 条方法均已签字且指纹一致")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="方法库签字工具")
    ap.add_argument(
        "--signoffs",
        type=Path,
        default=DEFAULT_SIGNOFFS_PATH,
        help=f"签字书路径 (默认 {DEFAULT_SIGNOFFS_PATH})",
    )
    ap.add_argument("--list", action="store_true", help="列出全部方法与签字状态")
    ap.add_argument("--show", metavar="METHOD_ID", help="显示一个方法的完整内容")
    ap.add_argument("--approve", metavar="METHOD_ID", help="签字通过")
    ap.add_argument("--by", default="", help="评审人 (配合 --approve)")
    ap.add_argument("--check", action="store_true", help="CI 门禁: 有未签方法则失败")
    args = ap.parse_args()

    if args.check:
        return cmd_check(args.signoffs)
    if args.approve:
        return cmd_approve(args.approve, args.by, args.signoffs)
    if args.show:
        return cmd_show(args.show, args.signoffs)
    return cmd_list(args.signoffs)


if __name__ == "__main__":
    sys.exit(main())
