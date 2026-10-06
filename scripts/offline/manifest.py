"""离线包清单: 每个文件的 sha256 + 版本事实 + 一份标准工具可校验的和文件。

**为什么要两份清单** (``MANIFEST.json`` + ``MANIFEST.sha256``)
-------------------------------------------------------------
``MANIFEST.json`` 是给人和程序读的(版本事实、每个文件的大小与摘要、
构建主机信息); ``MANIFEST.sha256`` 是给 ``sha256sum -c`` 读的。离线安装的
第一步就是 ``sha256sum -c``: 它不依赖 python、不依赖包里的任何代码, 所以
**包自己损坏时也能发现**。只有 JSON 清单的话, 校验就得先能跑起包里的
代码 —— 那是循环论证。

**清单不含自己与 tar 包**: 清单生成在打包之前, 所以它算不出自己的摘要
(自指)。tar 包的摘要在包外(``.tar.gz.sha256``), 由构建脚本最后写。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 清单不覆盖的文件: 清单自己、校验和文件本身, 以及临时文件。
SELF_EXCLUDED = ("MANIFEST.json", "MANIFEST.sha256")


def sha256_of(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _run(cmd: list[str]) -> str | None:
    """跑命令拿输出; 拿不到就返回 None —— **清单不许因为查不到版本而生成失败**。

    理由: 清单是安装前的校验依据。生成清单这一步失败, 包就没了, 而
    「查不到 semantica 版本号」这种小问题不该拦住交付; 该字段空着,
    反而比一个编出来的值诚实。
    """
    try:
        out = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def facts(index_url: str = "") -> dict[str, Any]:
    """版本事实。**只报事实, 不做判断**。"""
    info: dict[str, Any] = {
        "built_at_utc": datetime.now(UTC).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        # 轮子是从哪个源取的。清单保证「文件没被改过」, 不保证「当初从哪
        # 拿的」—— 后者是排障时第一个要看的线索, 所以必须记下来, 不能靠
        # 回忆构建命令来还原。
        "wheelhouse_index_url": index_url or None,
    }
    # 包里带的解释器是「运行时事实」, 装到目标机上要能对上, 所以记下来。
    for name, mod in (("semantica", "semantica"), ("psycopg", "psycopg")):
        try:
            info[f"{name}_version"] = __import__(mod).__version__
        except Exception:  # noqa: BLE001 # 查不到就空着, 见 _run 的理由
            info[f"{name}_version"] = None
    pg = _run(["psql", "--version"])
    info["postgresql_client"] = pg
    return info


def collect(root: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in SELF_EXCLUDED or rel.endswith(".pyc"):
            continue
        if "__pycache__" in rel:
            continue
        files[rel] = {"sha256": sha256_of(path), "size": path.stat().st_size}
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成离线包清单")
    ap.add_argument("--root", required=True, help="包根目录")
    ap.add_argument("--git-sha", default="unknown", help="源码 commit")
    ap.add_argument("--out-manifest", default="")
    ap.add_argument("--out-checksums", default="")
    ap.add_argument("--index-url", default="", help="轮子实际取自哪个源(排障线索)")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"包根目录不存在: {root}", file=sys.stderr)
        return 2
    out_manifest = Path(args.out_manifest) if args.out_manifest else root / "MANIFEST.json"
    out_checks = Path(args.out_checksums) if args.out_checksums else root / "MANIFEST.sha256"

    files = collect(root)
    manifest = {
        "schema": 1,
        "git_sha": args.git_sha,
        "facts": facts(args.index_url),
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files.values()),
        "files": files,
    }
    out_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    out_checks.write_text(
        "".join(f"{meta['sha256']}  {rel}\n" for rel, meta in files.items()),
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"清单: {len(files)} 个文件 / "
        f"{manifest['total_bytes'] / 1048576:.1f} MiB -> {out_manifest.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
