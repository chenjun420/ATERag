"""板卡 SSH 巡检: 连通性 + 运行环境 (部署 MCP systemd 前勘察).

用法: .venv\\Scripts\\python.exe scripts\\board_ssh_probe.py
凭据从环境变量读: BOARD_SSH_USER / BOARD_SSH_PASSWORD / BOARD_SSH_HOST
"""

from __future__ import annotations

import os
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_SSH_HOST", "192.168.5.24")
USER = os.environ.get("BOARD_SSH_USER", "")
PWD = os.getenv("BOARD_SSH_PASSWORD", "")

PROBES = [
    ("主机/架构", "hostname; uname -m; cat /etc/os-release | grep PRETTY"),
    ("sudo 权限", "sudo -n true 2>/dev/null && echo 'SUDO_NOPASSWD' || echo 'SUDO_NEEDS_PASSWORD'"),
    ("内存", "free -h | head -2"),
    ("磁盘", "df -h / /opt 2>/dev/null | tail -3"),
    (
        "运行服务",
        "systemctl list-units --type=service --state=running --no-pager --no-legend | grep -Ei 'qdrant|postgres|lightrag|aterag|mcp' || echo none",
    ),
    (
        "Python",
        "for v in python3 python3.11 python3.12 python3.13; do command -v $v >/dev/null && echo \"$v -> $($v -V 2>&1)\"; done; echo '--- uv ---'; command -v uv || echo no-uv",
    ),
    ("已有应用目录", "ls -la ~/aterag 2>/dev/null || echo 'no ~/aterag'"),
]


def main() -> int:
    if not PWD:
        print("未设置 BOARD_SSH_PASSWORD, 跳过 SSH 巡检")
        return 1
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(
            HOST, username=USER, password=PWD, timeout=15, look_for_keys=False, allow_agent=False
        )
    except Exception as e:  # noqa: BLE001
        print(f"SSH 连接失败 {USER}@{HOST}: {type(e).__name__}: {e}")
        return 1
    print(f"=== 板卡巡检 {USER}@{HOST} ===")
    for label, cmd in PROBES:
        _, out, err = cli.exec_command(cmd, timeout=30)
        text = (
            out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")
        ).strip()
        print(f"\n--- {label} ---\n{text[:800]}")
    cli.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
