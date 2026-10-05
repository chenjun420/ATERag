#!/bin/bash
# ATERag 板卡应用层部署: Python 3.13 (uv 预编译) + venv + 依赖 + systemd 常驻
# 目标机: 192.168.5.25 (Debian 12 ARM64)
# 前置: Step 1~5 已完成 (PostgreSQL 17 + AGE/vector/pg_textsearch/zhparser + Qdrant)
# 用法: sudo -i  然后  bash /opt/aterag/native/06-install-aterag.sh
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

APP_DIR=/opt/aterag
LOG_DIR=/var/log/aterag
SVC_USER=aterag
PY_HOME=/opt/python          # uv 预编译解释器 (共享可读, 不能放 /root/.local)
UV_BIN=/usr/local/bin/uv     # 系统级安装: 服务账号 aterag 必须能执行
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "===== STEP6.0 系统依赖 ====="
apt-get update -qq
apt-get install -y --no-install-recommends python3.11-venv ca-certificates curl git

echo "===== STEP6.1 uv + 预编译 CPython 3.13 ====="
if [ ! -x "$UV_BIN" ]; then
  # uv 官方安装器装到当前用户家目录 (root -> /root/.local), 服务账号无法穿越 /root,
  # 因此装完再拷到 /usr/local/bin 全局可执行
  curl -sSL --max-time 180 https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
  sh /tmp/uv-install.sh
  install -m 0755 /root/.local/bin/uv "$UV_BIN"
fi
"$UV_BIN" --version
mkdir -p "$PY_HOME"
chmod 755 "$PY_HOME"
# 关键: 解释器装到共享目录, 否则 venv 内的 python 指向 /root/.local, 服务账号无权访问
# --no-config: uv 会从 CWD 向上找 uv.toml/pyproject.toml; 服务账号读不了 SSH 用户家目录
UV_PYTHON_INSTALL_DIR="$PY_HOME" "$UV_BIN" --no-config python install 3.13
PY_BIN="$(ls -d "$PY_HOME"/cpython-3.13*/bin/python3.13 2>/dev/null | head -1)"
[ -x "$PY_BIN" ] || { echo "FATAL: 未找到 3.13 解释器"; exit 1; }
echo "解释器: $PY_BIN"
"$PY_BIN" -V

echo "===== STEP6.2 服务账号与目录 ====="
id -u "$SVC_USER" >/dev/null 2>&1 || useradd -r -m -d "$APP_DIR" -s /bin/bash "$SVC_USER"
mkdir -p "$APP_DIR" "$LOG_DIR"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR" "$LOG_DIR"

echo "===== STEP6.3 venv (CPython 3.13) ====="
rm -rf "$APP_DIR/.venv"
cd "$APP_DIR"   # 切到应用目录, 避免 uv 向上找到 SSH 用户家目录下的配置
UV_PYTHON_INSTALL_DIR="$PY_HOME" "$UV_BIN" --no-config venv --python 3.13 "$APP_DIR/.venv"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -V

echo "===== STEP6.4 安装 Python 依赖 ====="
# 从 pyproject.toml 安装, 不再在这里硬编码一份包列表。
#
# 这份列表曾经和 pyproject 并存, 于是有两处真相: 它漏掉了 psycopg2-binary
# (semantica.ApacheAgeStore 的硬依赖, 也是 sync_semantica.py 的唯一驱动路径),
# 也没跟上后来加的 fastapi / uvicorn / paramiko / numpy。板卡上装出来的环境
# 因此和开发机不同, 而差异不会报错, 只在用到那条路径时才炸。
#
# 单点真相是 pyproject.toml。
sudo -u "$SVC_USER" env UV_PYTHON_INSTALL_DIR="$PY_HOME" \
  "$UV_BIN" --no-config pip install --python "$APP_DIR/.venv/bin/python" \
  -e "$APP_DIR"

# 装完必须真的能导入, 尤其是那几个「只在运行时按名字加载」的:
# psycopg2 (semantica 的 AGE store)、pgvector+asyncpg (lightrag PGVectorStorage)、
# fastapi+uvicorn (workbench)、paramiko (板卡脚本)。
"$APP_DIR/.venv/bin/python" - <<'PYCHECK'
import importlib
mods = ("lightrag", "semantica", "pyshacl", "mcp", "psycopg", "psycopg2",
        "qdrant_client", "pgvector", "asyncpg", "numpy")
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as exc:
        bad.append(f"{m}: {type(exc).__name__}: {exc}")
if bad:
    raise SystemExit("依赖导入失败:\n  " + "\n  ".join(bad))
print("依赖导入 OK:", ", ".join(mods))
PYCHECK

echo "===== STEP6.5 systemd ====="
cp "$SCRIPT_DIR/aterag-mcp.service" /etc/systemd/system/aterag-mcp.service
systemctl daemon-reload
systemctl enable aterag-mcp.service
systemctl restart aterag-mcp.service
sleep 10
systemctl is-active aterag-mcp.service
systemctl is-enabled aterag-mcp.service

echo "===== STEP6.6 健康检查 ====="
PORT="$(grep -E '^MCP_PORT=' "$APP_DIR/.env" | cut -d= -f2)"
ss -ltn | grep ":${PORT}" || echo "WARN: ${PORT} 未监听"
curl -sS -o /dev/null -w 'mcp HTTP=%{http_code}\n' --max-time 15 "http://127.0.0.1:${PORT}/mcp" || true
echo "--- 错误日志尾部 ---"
tail -20 "$LOG_DIR/mcp.err" || true

echo "== STEP6 DONE =="
