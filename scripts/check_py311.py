"""源码 Python 3.11 兼容性静态检查 (板卡只有 3.11.2).

用 ast.parse(feature_version=(3,11)) 逐文件解析, 检出 3.12+ 语法 (PEP 695 type
语句/泛型, 各类 f-string 放宽等), 以及 3.11 已移除的标准库别名。

用法: .venv\\Scripts\\python.exe scripts\\check_py311.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 3.12 移除 / 3.13 移除 的标准库别名 -> 3.11 及更早可用别名
REMOVED_ALIASES = {
    "distutils": "setuptools._distutils 或改用 packaging",
    "imp": "importlib.util / importlib.metadata",
    "asynchat": "asyncio (已废弃)",
    "asyncore": "asyncio (已废弃)",
    "smtpd": "aiosmtpd",
}


def main() -> int:
    root = Path("src") if Path("src").exists() else Path("src/aterag")
    files = sorted(root.rglob("*.py")) + sorted(Path("scripts").glob("*.py"))
    bad: list[str] = []
    alias_hits: list[str] = []

    for f in files:
        src = f.read_text(encoding="utf-8")
        try:
            ast.parse(src, filename=str(f), feature_version=(3, 11))
        except SyntaxError as e:
            bad.append(f"{f}:{e.lineno}: {e.msg}")
        for mod, advice in REMOVED_ALIASES.items():
            if f"import {mod}" in src or f"from {mod}" in src:
                alias_hits.append(f"{f}: {mod} -> {advice}")

    for line in bad:
        print(f"  [SYNTAX-3.11] {line}")
    for line in alias_hits:
        print(f"  [ALIAS-REMOVED] {line}")
    print(f"检查文件数: {len(files)}  3.11 语法不兼容: {len(bad)}  移除别名: {len(alias_hits)}")
    print("PY311_CHECK", "PASS" if not bad and not alias_hits else "FAIL")
    return 0 if not bad and not alias_hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
