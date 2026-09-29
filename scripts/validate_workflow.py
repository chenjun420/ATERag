"""校验 GitHub Actions 工作流 YAML (提交前必跑).

背景: 曾两次推送失败的工作流 —— 根因是 `- name: Rule self-test (117 rules: derive + SHACL)`
中的 ": " 被 YAML 当作键值分隔符, GitHub 直接判定 "Invalid workflow file", 流水线从未真正执行。
本脚本把这类错误挡在本地。

用法: .venv\\Scripts\\python.exe scripts/validate_workflow.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")

# YAML 裸标量中 ": " 会被当作 key: value 分隔, 出现在 - name: 后面必须加引号
NAME_RE = re.compile(r"^\s*(?:-\s+)?name:\s+(?P<val>[^\"'].*:\s.*)$")
QUOTED = re.compile(r"""^\s*(?:-\s+)?name:\s+["'].*["']\s*$""")


def main() -> int:
    wf = Path(".github/workflows/ci.yml")
    if not wf.exists():
        print("未找到 .github/workflows/ci.yml")
        return 1
    src = wf.read_text(encoding="utf-8")
    problems: list[str] = []

    # 1. YAML 必须能解析
    try:
        doc = yaml.safe_load(src)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"第 {mark.line + 1} 行 第 {mark.column + 1} 列" if mark else "?"
        print(f"[FAIL] YAML 解析失败 ({where})")
        print("      " + str(e).replace("\n", "\n      "))
        return 1
    print("[PASS] YAML 解析通过")

    # 2. 必填字段
    jobs = doc.get("jobs") or {}
    if not jobs:
        problems.append("没有定义任何 job")
    for name, job in jobs.items():
        if "runs-on" not in job:
            problems.append(f"job {name} 缺 runs-on")
        if not job.get("steps"):
            problems.append(f"job {name} 没有 steps")

    # 3. 扫描未加引号却含 ": " 的 name (GitHub 会判定 Invalid workflow file)
    for i, line in enumerate(src.splitlines(), 1):
        if QUOTED.match(line):
            continue
        m = NAME_RE.match(line)
        if m:
            problems.append(
                f"第 {i} 行 name 未加引号且含 ': ' -> Invalid workflow file: {line.strip()}"
            )

    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        print(f"\nWORKFLOW_VALIDATE FAIL ({len(problems)} 项)")
        return 1

    print(f"[PASS] jobs: {list(jobs)}")
    print(f"[PASS] 共 {len(src.splitlines())} 行, 无语法隐患")
    print("\nWORKFLOW_VALIDATE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
