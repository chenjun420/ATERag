"""板卡 SSH 命令执行器 (密码认证) — 部署/排障通用工具.

用法 (单次):
    $env:BOARD_SSH_PASSWORD='xxx'
    .venv\\Scripts\\python.exe scripts\\board_ssh.py "uname -a"
    .venv\\Scripts\\python.exe scripts\\board_ssh.py --sudo "systemctl status postgresql"
    .venv\\Scripts\\python.exe scripts\\board_ssh.py --put local.txt /remote/path
    .venv\\Scripts\\python.exe scripts\\board_ssh.py --get /remote/path local.txt

凭据: BOARD_SSH_HOST / BOARD_SSH_USER / BOARD_SSH_PASSWORD
注意: 密码只从环境变量读, 绝不写入仓库。
"""

from __future__ import annotations

import os
import posixpath
import shlex
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_SSH_HOST", "192.168.5.24")
USER = os.environ.get("BOARD_SSH_USER", "")
PWD = os.getenv("BOARD_SSH_PASSWORD", "")


def connect() -> paramiko.SSHClient:
    if not PWD:
        sys.exit("未设置 BOARD_SSH_PASSWORD")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        HOST, username=USER, password=PWD, timeout=20, look_for_keys=False, allow_agent=False
    )
    return cli


def run_cmd(cmd: str, use_sudo: bool = False) -> int:
    cli = connect()
    try:
        # 关键: sudo 必须包裹整条命令链。`sudo -n a && b` 只给 a 提权, b 仍以 SSH
        # 用户身份运行 (chown 就会报 "Operation not permitted")。
        full = f"sudo -n sh -c {shlex.quote(cmd)}" if use_sudo else cmd
        _, out, err = cli.exec_command(full, timeout=1800)
        so = out.read().decode("utf-8", "replace")
        se = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        if so:
            sys.stdout.write(so)
        if se:
            sys.stderr.write(se)
        return rc
    finally:
        cli.close()


def put(local: str, remote: str) -> int:
    cli = connect()
    try:
        sftp = cli.open_sftp()
        try:
            parent = posixpath.dirname(remote)
            if parent:
                cli.exec_command(f"mkdir -p {parent}")[1].channel.recv_exit_status()
            sftp.put(local, remote)
        finally:
            sftp.close()
        print(f"PUT {local} -> {remote}")
        return 0
    finally:
        cli.close()


def get(remote: str, local: str) -> int:
    cli = connect()
    try:
        sftp = cli.open_sftp()
        try:
            sftp.get(remote, local)
        finally:
            sftp.close()
        print(f"GET {remote} -> {local}")
        return 0
    finally:
        cli.close()


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    if args[0] == "--put" and len(args) == 3:
        return put(args[1], args[2])
    if args[0] == "--get" and len(args) == 3:
        return get(args[1], args[2])
    use_sudo = False
    if args[0] == "--sudo":
        use_sudo, args = True, args[1:]
    return run_cmd(" ".join(args), use_sudo)


if __name__ == "__main__":
    raise SystemExit(main())
