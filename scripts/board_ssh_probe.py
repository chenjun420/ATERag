"""板卡 SSH 巡检: 连通性 + 运行环境 (部署 MCP systemd 前勘察).

用法: .venv\\Scripts\\python.exe scripts\\board_ssh_probe.py
凭据由 board_ssh 统一解析 (BOARD_SSH_HOST / _USER / _KEY / _PASSWORD, key 优先)
"""

from __future__ import annotations

import sys

from board_ssh import HOST, KEY, PWD, USER
from board_ssh import connect as board_connect

sys.stdout.reconfigure(encoding="utf-8")


PROBES = [
    ("主机/架构", "hostname; uname -m; cat /etc/os-release | grep PRETTY"),
    ("sudo 权限", "sudo -n true 2>/dev/null && echo 'SUDO_NOPASSWD' || echo 'SUDO_NEEDS_PASSWORD'"),
    ("内存", "free -h | head -2"),
    ("磁盘", "df -h / /opt 2>/dev/null | tail -3"),
    (
        "运行服务",
        "systemctl list-units --type=service --state=running --no-pager --no-legend | grep -Ei 'postgres|aterag|mcp' || echo none",
    ),
    (
        "Python",
        "for v in python3 python3.11 python3.12 python3.13; do command -v $v >/dev/null && echo \"$v -> $($v -V 2>&1)\"; done; echo '--- uv ---'; command -v uv || echo no-uv",
    ),
    ("已有应用目录", "ls -la ~/aterag 2>/dev/null || echo 'no ~/aterag'"),
]


def main() -> int:
    if not PWD and not KEY:
        print("无可用 SSH 凭据 (设 BOARD_SSH_KEY 或 BOARD_SSH_PASSWORD), 跳过巡检")
        return 1
    try:
        cli = board_connect()
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
