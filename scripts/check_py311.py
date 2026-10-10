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


def _removed_alias_hits(src: str, filename: str) -> list[str]:
    """按 AST 判定「真的 import 了被移除的模块」。

    早先用 ``f"import {mod}" in src`` 做子串匹配, 于是 ``import importlib.metadata``
    里�� ``import imp`` 子串而被误报 ``imp -> importlib.util`` —— 而 ``importlib``
    在 3.11 完全正常。那种误报比漏报更糟: 它逼着人把正常的 import 改名/绕写去迎合
    一个并不存在的规则, 于是检查器开始制造它本该防的问题。

    正确判据是**导入语句的模块名**: ``import X`` / ``import X.Y`` 里 X 是被移除的
    顶层名, 或 ``from X import ...`` 的 X 是被移除的顶层名。
    """
    try:
        tree = ast.parse(src, filename=str(filename))
    except SyntaxError:
        return []  # 语法错由上面的 3.11 解析单独报, 这里不重复
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in REMOVED_ALIASES:
                    hits.append(f"{top} -> {REMOVED_ALIASES[top]}")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top in REMOVED_ALIASES:
                hits.append(f"{top} -> {REMOVED_ALIASES[top]}")
    return hits


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
        alias_hits.extend(f"{f}: {msg}" for msg in _removed_alias_hits(src, f))

    for line in bad:
        print(f"  [SYNTAX-3.11] {line}")
    for line in alias_hits:
        print(f"  [ALIAS-REMOVED] {line}")
    print(f"检查文件数: {len(files)}  3.11 语法不兼容: {len(bad)}  移除别名: {len(alias_hits)}")
    print("PY311_CHECK", "PASS" if not bad and not alias_hits else "FAIL")
    return 0 if not bad and not alias_hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
