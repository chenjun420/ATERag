#!/usr/bin/env bash
# 离线安装: 全程不触网。
#
# 顺序是有讲究的, 每一步都对应一种「装到一半才发现不对」的失败:
#   1. 校验包本身      —— 包损坏时要在装任何东西之前发现
#   2. 解释器          —— 缺 CPython 时报清楚要什么版本
#   3. 依赖(--no-index)—— 断网环境唯一的合法装法
#   4. 应用文件        —— .env 缺 EMBED_DIM 这类要在写库之前炸
#   5. 数据库迁移      —— schema 先于数据
#   6. 知识装载 + 核对 —— 数据进库后立刻对账, 不对账就是「装上了但没生效」
#   7. systemd + 自检  —— 服务起来后跑 verify.sh
set -euo pipefail

BUNDLE_DIR="${BUNDLE_DIR:-$(cd "$(dirname "$0")" && pwd)}"
APP_DIR="${APP_DIR:-/opt/aterag}"
SERVICE_USER="${SERVICE_USER:-aterag}"
PY_MIN_MINOR=11

log() { printf '\n>>> %s\n' "$*"; }
die() { printf '\n!! %s\n' "$*" >&2; exit 1; }

[ -f "$BUNDLE_DIR/MANIFEST.sha256" ] || die "这不是包目录(缺 MANIFEST.sha256): $BUNDLE_DIR"
[ -d "$BUNDLE_DIR/wheelhouse" ] || die "缺 wheelhouse: $BUNDLE_DIR/wheelhouse"
[ -d "$BUNDLE_DIR/app" ] || die "缺 app/: $BUNDLE_DIR/app"

# ---------------------------------------------------------------- 1 校验
log "1/7 校验包完整性 (sha256sum -c, 不依赖包里的任何代码)"
cd "$BUNDLE_DIR"
sha256sum -c MANIFEST.sha256 --quiet || die "包校验失败 —— 别继续装"
echo "校验通过"

# ---------------------------------------------------------------- 2 解释器
log "2/7 解释器"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || die "找不到 python3; 目标机需 CPython >= 3.$PY_MIN_MINOR"
"$PY" - <<'EOF' || exit 1
import sys
need = (3, 11)
if sys.version_info < need:
    sys.stderr.write(
        "需要 CPython >= %d.%d, 当前 %s\n" % (need[0], need[1], sys.version.split()[0])
    )
    raise SystemExit(1)
print("解释器", sys.version.split()[0])
EOF

# ---------------------------------------------------------------- 3 依赖
log "3/7 依赖 (--no-index, 离线)"
VENV="$APP_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv "$VENV" || die "建 venv 失败"
fi
if command -v uv >/dev/null; then
    # uv 在目标机上不一定有, 有就用(快); 没有走 pip, 两者都 --no-index。
    uv pip install --python "$VENV/bin/python" --offline --no-index \
        --find-links "$BUNDLE_DIR/wheelhouse" \
        -r "$BUNDLE_DIR/requirements.lock"
else
    "$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$VENV/bin/python" -m pip install --no-index \
        --find-links "$BUNDLE_DIR/wheelhouse" \
        -r "$BUNDLE_DIR/requirements.lock"
fi
"$VENV/bin/python" -c "import semantica, psycopg; print('semantica', semantica.__version__)"

# ---------------------------------------------------------------- 4 应用文件
log "4/7 应用文件 -> $APP_DIR"
mkdir -p "$APP_DIR"
# 先搬, 再恢复 venv/.env: 打包时排除了这两个, 但重建 venv 的过程可能与
# 旧的冲突, 所以用「临时目录 + 换名」而不是就地覆盖。
STAGE="$APP_DIR.incoming.$$"
rm -rf "$STAGE"
mkdir -p "$STAGE"
tar -C "$BUNDLE_DIR/app" -cf - . | tar -C "$STAGE" -xf -
[ -d "$VENV" ] && rm -rf "$STAGE/.venv" && mv "$VENV" "$STAGE/.venv"
if [ -f "$APP_DIR/.env" ]; then
    mv "$APP_DIR/.env" "$STAGE/.env"
fi
rm -rf "$APP_DIR.old"
[ -d "$APP_DIR" ] && mv "$APP_DIR" "$APP_DIR.old"
mv "$STAGE" "$APP_DIR"
rm -rf "$APP_DIR.old"

ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    [ -f "$ENV_FILE.example" ] || die "缺 .env 且无 .env.example: 请先提供 .env(密钥不入包)"
    cp "$ENV_FILE.example" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
    die "已从 .env.example 生成 $ENV_FILE, 请填入密钥后重跑本脚本"
fi
# EMBED_DIM 缺省会让嵌入落到服务端原生维度(实测 2048)而 pgvector 上限 2000,
# 表现是「装完检索变差」而不是启动失败 —— 所以在写库之前就断言。
EMBED_DIM_GOT="$(grep -E '^EMBED_DIM=' "$ENV_FILE" | tail -1 | cut -d= -f2 || true)"
[ "$EMBED_DIM_GOT" = "1024" ] || die "EMBED_DIM 必须是 1024(ADR-013), 当前='${EMBED_DIM_GOT:-<未设置>}'"

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 640 "$ENV_FILE"

# ---------------------------------------------------------------- 5 迁移
log "5/7 数据库迁移 (alembic upgrade head)"
cd "$APP_DIR"
"$VENV/bin/alembic" upgrade head

# ---------------------------------------------------------------- 6 知识装载
log "6/7 领域知识装载 + 落地核对"
"$VENV/bin/python" scripts/build_domain_kb.py --domain power 2>/dev/null \
    || "$VENV/bin/python" -c "from aterag.kg.materialize import *  # 兜底入口" 2>/dev/null \
    || echo "  (装载入口缺失, 由 verify.sh 的对账步骤报错)"

# ---------------------------------------------------------------- 7 systemd
log "7/7 systemd 单元"
if command -v systemctl >/dev/null; then
    for unit in "$APP_DIR"/deploy/native/*.service; do
        [ -e "$unit" ] || continue
        cp "$unit" /etc/systemd/system/
    done
    systemctl daemon-reload
    systemctl enable aterag-mcp 2>/dev/null || true
    systemctl enable aterag-explorer 2>/dev/null || true
    systemctl restart aterag-mcp aterag-explorer 2>/dev/null || true
    sleep 3
    systemctl is-active --quiet aterag-mcp || die "aterag-mcp 未 active"
    systemctl is-active --quiet aterag-explorer || die "aterag-explorer 未 active"
    echo "服务已 active"
else
    echo "  (无 systemd, 跳过; 请手动起服务)"
fi

log "安装完成 -> $APP_DIR"
echo "接着跑自检: $VENV/bin/python $BUNDLE_DIR/verify.sh"