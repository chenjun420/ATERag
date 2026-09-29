"""板卡应用层部署: 上传源码 + .env + systemd unit -> 执行 Step6 -> 验证常驻.

用法:
    $env:BOARD_SSH_PASSWORD='<板卡 SSH 密码>'
    .venv\\Scripts\\python.exe scripts\\deploy_mcp_board.py

幂等: 重复执行只覆盖文件, 不影响已入库知识。
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
import sys
import time

import paramiko

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_SSH_HOST", "192.168.5.24")
USER = os.environ.get("BOARD_SSH_USER", "")
PWD = os.getenv("BOARD_SSH_PASSWORD", "")
APP_DIR = "/opt/aterag"
PORT = "8080"

# (本地路径, 板卡远端相对路径)
FILES = [
    ("src/aterag", "src/aterag"),
    ("domain_rules", "domain_rules"),
    ("registry.yaml", "registry.yaml"),
    ("pyproject.toml", "pyproject.toml"),
    # 板卡侧新规格书导入 CLI (upload_new_spec.py 远程调用的入口)
    ("scripts/ingest_new_spec.py", "scripts/ingest_new_spec.py"),
    ("deploy/native/aterag-mcp.service", "native/aterag-mcp.service"),
    ("deploy/native/06-install-aterag.sh", "native/06-install-aterag.sh"),
    ("deploy/native/06a-install-uv-python313.sh", "native/06a-install-uv-python313.sh"),
]

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def board_env(local_env: str) -> str:
    """生成板卡侧 .env: 密钥沿用开发机, 存储端点改指本机回环.

    MCP 服务现在跑在板卡上, 再用 192.168.5.24 自指没有必要且依赖网卡地址;
    统一改 127.0.0.1 更快也更抗 IP 变更。
    """
    txt = open(local_env, encoding="utf-8").read()
    txt = re.sub(r"^(POSTGRES_DSN=postgresql://[^:@]+:[^@]+@)[^:/]+", r"\g<1>127.0.0.1", txt, flags=re.MULTILINE)
    txt = re.sub(r"^(QDRANT_URL=https?://)[^:/]+", r"\g<1>127.0.0.1", txt, flags=re.MULTILINE)
    txt = re.sub(r"^(MCP_HOST=).*$", r"\g<1>0.0.0.0", txt, flags=re.MULTILINE)
    txt = re.sub(r"^(MCP_PORT=).*$", rf"\g<1>{PORT}", txt, flags=re.MULTILINE)
    if "MCP_PORT=" not in txt:
        txt += f"\nMCP_HOST=0.0.0.0\nMCP_PORT={PORT}\n"
    return "# ATERag 板卡运行时配置 (由 scripts/deploy_mcp_board.py 生成, 存储端点=127.0.0.1)\n" + txt


def upload_tree(sftp, local: str, remote: str) -> int:
    n = 0
    for root, dirs, files in os.walk(local):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel = os.path.relpath(root, local).replace("\\", "/")
        rdir = remote if rel == "." else posixpath.join(remote, rel)
        try:
            sftp.mkdir(rdir)
        except OSError as e:
            # 已存在可忽略; 权限不足必须暴露 (否则后续 put 报 ENOENT, 根因难查)
            if not sftp.stat(rdir):
                raise RuntimeError(f"无法创建远端目录 {rdir}: {e}") from e
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            sftp.put(os.path.join(root, f), posixpath.join(rdir, f))
            n += 1
    return n


def run(cli: paramiko.SSHClient, cmd: str, timeout: int = 2400, sudo: bool = False) -> int:
    # sudo 必须包裹整条命令链: `sudo -n a && b` 只提权 a, b 仍以 SSH 用户身份跑
    full = f"sudo -n sh -c {shlex.quote(cmd)}" if sudo else cmd
    _, out, err = cli.exec_command(full, timeout=timeout)
    so = out.read().decode("utf-8", "replace")
    se = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    for ln in so.strip().splitlines()[-45:]:
        print("    " + ln[:200])
    if rc != 0:
        for ln in se.strip().splitlines()[-15:]:
            print("    ERR " + ln[:200])
    return rc


def main() -> int:
    if not PWD:
        print("未设置 BOARD_SSH_PASSWORD")
        return 1
    print(f"=== 板卡应用层部署 -> {USER}@{HOST}:{APP_DIR} ===")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=20, look_for_keys=False, allow_agent=False)
    try:
        # SFTP 以 SSH 用户身份写入, /opt/aterag 默认属 root -> 先授权给部署用户,
        # 安装脚本 (sudo) 会在最后把属主改回服务账号 aterag
        run(cli, f"mkdir -p {APP_DIR}/src {APP_DIR}/native && chown -R {USER}:{USER} {APP_DIR}", sudo=True)

        sftp = cli.open_sftp()
        try:
            total = 0
            for local, remote in FILES:
                r = posixpath.join(APP_DIR, remote)
                if os.path.isdir(local):
                    n = upload_tree(sftp, local, r)
                    print(f"  [PUT] {local} -> {r} ({n} 文件)")
                    total += n
                else:
                    # 单文件: 先确保父目录存在 (SFTP 无 mkdir -p)
                    parent = posixpath.dirname(r)
                    try:
                        sftp.mkdir(parent)
                    except OSError:
                        pass
                    sftp.put(local, r)
                    print(f"  [PUT] {local} -> {r}")
                    total += 1
            # .env 单独生成 (端点改回环)
            with sftp.open(posixpath.join(APP_DIR, ".env"), "w") as fh:
                fh.write(board_env(".env"))
            print(f"  [PUT] .env -> {APP_DIR}/.env (端点改 127.0.0.1)")
        finally:
            sftp.close()
        print(f"  共上传 {total + 1} 项")

        # .env 含 API Key -> 600, 且必须属服务账号
        run(cli, f"chmod 600 {APP_DIR}/.env; chmod +x {APP_DIR}/native/*.sh", sudo=True)

        print("\n>>> 板卡 Step6 安装 (apt + uv + CPython3.13 + venv + 依赖 + systemd)")
        t0 = time.time()
        rc = run(cli, f"bash {APP_DIR}/native/06-install-aterag.sh", timeout=3000, sudo=True)
        print(f"    rc={rc} ({time.time()-t0:.0f}s)")
        if rc != 0:
            print("DEPLOY_MCP_BOARD FAIL (安装步骤)")
            return 1

        print("\n>>> 服务状态与健康")
        run(cli, "systemctl is-enabled aterag-mcp; systemctl is-active aterag-mcp", sudo=True)
        run(cli, f"ss -ltn | grep ':{PORT}' || echo '{PORT} 未监听'", sudo=True)
        run(cli, "/opt/aterag/.venv/bin/python -V; echo '--- 错误日志 ---'; tail -25 /var/log/aterag/mcp.err || true", sudo=True)
    finally:
        cli.close()
    print("DEPLOY_MCP_BOARD PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
