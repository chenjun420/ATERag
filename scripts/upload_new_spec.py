"""新产品规格书一键上传 + 导入 (开发机 -> 板卡 -> 全链路入库).

把本地 Markdown 规格书上传到板卡, 调用板卡 venv 执行 ingest_spec 全链路
(型号识别 -> 类型分类 -> 注册 -> 解析 -> 实体抽取 -> PG/Qdrant/LightRAG),
最后回读校验。

用法:
    $env:BOARD_SSH_PASSWORD='<板卡 SSH 密码>'
    .venv\\Scripts\\python.exe scripts\\upload_new_spec.py "D:/docs/PN2000-24A 规格书.md"

选项:
    --dry-run     只做前置校验与上传, 不执行导入
    --local       改为在开发机本地导入 (不经过板卡, 便于快速迭代)
"""
from __future__ import annotations

import argparse
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time

import paramiko

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_SSH_HOST", "192.168.5.24")
USER = os.environ.get("BOARD_SSH_USER", "")
PWD = os.getenv("BOARD_SSH_PASSWORD", "")
SPECS_DIR = "/opt/aterag/specs"
PY = "/opt/aterag/.venv/bin/python"
INGEST_CLI = "/opt/aterag/scripts/ingest_new_spec.py"

# 必须与 src/aterag/ingest/pipeline.py::ingest_spec 中的识别正则保持一致
MODEL_ID_RE = re.compile(r"\b([A-Z]{2,8}\d[A-Z0-9]*(?:-[A-Z0-9]+)+)\b")
VERSION_RE = re.compile(r"版本[:：]\s*([A-Z0-9.]+)")


def precheck(path: str) -> tuple[str, str, list[str]]:
    """导入前校验: 文件/编码/型号 ID/版本 是否满足 pipeline 的识别前提。"""
    problems: list[str] = []
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        return p, "", [f"文件不存在: {p}"]
    raw = open(p, "rb").read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return p, "", [f"非 UTF-8 编码 (ingest_spec 以 utf-8 读取): {e}"]
    if not p.lower().endswith((".md", ".markdown")):
        problems.append("扩展名非 .md/.markdown —— 当前管道仅支持 Markdown, Word/Excel 需先转换")
    head = text[:500]
    m = MODEL_ID_RE.search(head)
    if not m:
        problems.append("前 500 字符内未匹配到型号 ID (需形如 PA601-D54A / PN2000-24A: 2~8 位大写字母+数字+连字符段)")
    vm = VERSION_RE.search(text[:800])
    if not vm:
        problems.append("前 800 字符内未匹配到 '版本: X' (可选, 但缺失则 doc_version 为空)")
    if not text.lstrip().startswith("#"):
        problems.append("首行不是 Markdown 标题, 章节树与分块质量会下降")
    return p, (m.group(1) if m else ""), problems


def sh(cli: paramiko.SSHClient, cmd: str, sudo: bool = False, timeout: int = 3600) -> tuple[int, str]:
    full = f"sudo -n sh -c {shlex.quote(cmd)}" if sudo else cmd
    _, out, err = cli.exec_command(full, timeout=timeout)
    so = out.read().decode("utf-8", "replace")
    se = err.read().decode("utf-8", "replace")
    return out.channel.recv_exit_status(), (so + se).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("doc", help="本地规格书路径 (.md)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--local", action="store_true", help="在开发机本地导入, 不上传板卡")
    args = ap.parse_args()

    path, model_id, problems = precheck(args.doc)
    print(f"=== 新规格书上传导入 ===\n文件: {path}")
    print(f"识别型号: {model_id or '(未识别)'}")
    for p in problems:
        print(f"  [WARN] {p}")
    if problems and not args.local:
        # 型号识别失败会让 ingest_spec 直接抛错, 没必要传上去
        hard = [p for p in problems if "未匹配到型号 ID" in p or "文件不存在" in p or "非 UTF-8" in p]
        if hard:
            print("\nUPLOAD_NEW_SPEC FAIL (前置校验不通过)")
            for p in hard:
                print(f"  - {p}")
            return 1

    if args.local:
        cmd = [sys.executable, "scripts/ingest_new_spec.py", path]
        print(f"\n>>> 本地导入: {' '.join(cmd[1:])}")
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(proc.stdout[-3000:])
        if proc.returncode != 0:
            print(proc.stderr[-1500:])
        return proc.returncode

    if not PWD:
        print("未设置 BOARD_SSH_PASSWORD")
        return 1

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=20, look_for_keys=False, allow_agent=False)
    try:
        remote = posixpath.join(SPECS_DIR, os.path.basename(path))
        print(f"\n>>> 上传 -> {USER}@{HOST}:{remote}")
        sftp = cli.open_sftp()
        try:
            sh(cli, f"mkdir -p {SPECS_DIR} && chown -R {USER}:{USER} {SPECS_DIR}", sudo=True)
            sftp.put(path, remote)
        finally:
            sftp.close()
        print(f"    上传完成 ({os.path.getsize(path)} 字节)")

        if args.dry_run:
            print("DRY RUN: 跳过导入")
            return 0

        print("\n>>> 板卡执行导入 (venv Python 3.13)")
        t0 = time.time()
        rc, out = sh(cli, f"{PY} {INGEST_CLI} {shlex.quote(remote)}", sudo=True)
        for ln in out.splitlines()[-25:]:
            print("    " + ln[:200])
        print(f"    rc={rc} ({time.time()-t0:.0f}s)")
        if rc != 0:
            print("UPLOAD_NEW_SPEC FAIL (导入失败)")
            return 1

        # 注册表是 MCP 服务启动时加载的 (server.py 模块级 Registry.load),
        # 新型号必须重启服务才会出现在 list_models / 查询路由里, 否则"导入成功但查不到"
        print("\n>>> 重启 MCP 服务 (加载新注册表)")
        restart_rc, out2 = sh(cli, "systemctl restart aterag-mcp && sleep 8 && systemctl is-active aterag-mcp", sudo=True)
        print("    " + out2.replace("\n", " | ")[-120:])
        # 服务未成功 active 即视为失败: 否则后续查询会因旧注册表而"查不到"
        if restart_rc != 0 or "active" not in out2:
            print("    FAIL: MCP 服务未成功重启")
            print("\nUPLOAD_NEW_SPEC FAIL (服务重启失败)")
            return 1

        # 回读板卡注册表, 确认型号已落盘
        _, reg = sh(cli, "cat /opt/aterag/registry.yaml")
        if model_id and model_id in reg:
            print(f"    注册表已含 {model_id}")
        else:
            # fail-closed: 注册表未落盘就不能算导入成功, 否则后续"导入成功但查不到"
            print(f"    FAIL: 注册表未含 {model_id}")
            print("\nUPLOAD_NEW_SPEC FAIL (注册表未落盘新型号)")
            return 1

        # 同步回开发机 (板卡为注册表唯一权威源)
        try:
            sftp = cli.open_sftp()
            try:
                sftp.get("/opt/aterag/registry.yaml", "registry.yaml")
                print("    registry.yaml 已同步回开发机")
            finally:
                sftp.close()
        except Exception as e:  # noqa: BLE001
            print(f"    WARN: 同步注册表失败 {e}")
    finally:
        cli.close()

    print("\nUPLOAD_NEW_SPEC PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
