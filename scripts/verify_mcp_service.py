"""板卡 MCP 服务 systemd 状态验证 (常驻 + 端点 + 版本 + 自启).

用法: .venv\\Scripts\\python.exe scripts\\verify_mcp_service.py
凭据: BOARD_SSH_PASSWORD
"""

from __future__ import annotations

import os
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.getenv("BOARD_SSH_HOST", "192.168.5.25")
USER = os.environ.get("BOARD_SSH_USER", "")
PWD = os.getenv("BOARD_SSH_PASSWORD", "")
SVC = "aterag-mcp"
PORT = "8080"


def sh(cli: paramiko.SSHClient, cmd: str, sudo: bool = False) -> tuple[int, str]:
    import shlex

    full = f"sudo -n sh -c {shlex.quote(cmd)}" if sudo else cmd
    _, out, err = cli.exec_command(full, timeout=180)
    so = out.read().decode("utf-8", "replace")
    se = err.read().decode("utf-8", "replace")
    return out.channel.recv_exit_status(), (so + se).strip()


def main() -> int:
    if not PWD:
        print("未设置 BOARD_SSH_PASSWORD")
        return 1
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        HOST, username=USER, password=PWD, timeout=20, look_for_keys=False, allow_agent=False
    )
    checks: list[tuple[str, bool, str]] = []
    try:
        _, active = sh(cli, f"systemctl is-active {SVC}", sudo=True)
        checks.append(("服务 active", active.strip() == "active", active.strip()))

        _, enabled = sh(cli, f"systemctl is-enabled {SVC}", sudo=True)
        checks.append(("开机自启 enabled", enabled.strip() == "enabled", enabled.strip()))

        _, restart = sh(cli, f"systemctl show {SVC} -p NRestarts --value", sudo=True)
        # 重启次数过多说明崩溃循环 (Restart=always)
        checks.append(
            (
                "无崩溃循环 (NRestarts<=2)",
                restart.strip().isdigit() and int(restart.strip()) <= 2,
                f"NRestarts={restart.strip()}",
            )
        )

        _, listen = sh(cli, f"ss -ltn | grep ':{PORT}' || true", sudo=True)
        checks.append(
            (
                f"端口 {PORT} 监听",
                f":{PORT}" in listen,
                listen.split("\n")[0][:60] if listen else "无",
            )
        )

        _, ver = sh(cli, "/opt/aterag/.venv/bin/python -V", sudo=True)
        checks.append(("venv Python 3.13", "3.13" in ver, ver.strip()))

        _, runas = sh(cli, f"systemctl show {SVC} -p User --value", sudo=True)
        checks.append(("以专用账号运行", runas.strip() == "aterag", f"User={runas.strip()}"))

        # MCP 端点: streamable-http 对裸 GET 返回 400 属正常, 关键是端口有响应
        _, http = sh(
            cli,
            f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 15 http://127.0.0.1:{PORT}/mcp",
        )
        checks.append(
            ("MCP 端点有响应", http.strip() in ("200", "400", "406"), f"HTTP={http.strip()}")
        )

        _, logs = sh(cli, "tail -5 /var/log/aterag/mcp.err || true", sudo=True)
        checks.append(
            (
                "无启动异常",
                "Traceback" not in logs and "Error" not in logs,
                "无 Traceback" if "Traceback" not in logs else "有 Traceback",
            )
        )
    finally:
        cli.close()

    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:32s} {detail}")
    n = sum(1 for _, ok, _ in checks if ok)
    print(f"MCP_SERVICE_VERIFY {'PASS' if n == len(checks) else 'FAIL'} {n}/{len(checks)}")
    return 0 if n == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
