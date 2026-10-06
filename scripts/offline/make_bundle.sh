#!/usr/bin/env bash
# 在板卡上组装离线版本包。
#
# 为什么在板卡上组装而不是在开发机: wheelhouse 必须是 **aarch64** 的轮子。
# 开发机是 Windows x86_64, 交叉编译带 C/Rust 扩展的轮子(psycopg-c、
# pydantic-core)不现实; 板卡本身就是 aarch64, 在这里 ``pip download``
# 拿到的就是目标平台的轮子。
#
# 产出的包是**可重放**的: 同一 commit + 同一依赖锁 -> 同一份包,
# 且安装过程不触网(--no-index)。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aterag}"
OUT_ROOT="${OUT_ROOT:-/opt/aterag-bundle}"
GIT_SHA="${1:?用法: make_bundle.sh <git-sha>}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE="$OUT_ROOT/aterag-offline-$STAMP-$GIT_SHA"

log() { printf '\n>>> %s\n' "$*"; }

mkdir -p "$BUNDLE"

# ---------------------------------------------------------------- 源码
# 从已部署的树取源码(板卡上没有 .git; 部署用的是 git archive 的产物,
# 所以板卡的树**就是**那个 commit 的内容)。--exclude 掉运行期垃圾与密钥。
log "复制源码树 (来自 $APP_DIR, commit $GIT_SHA)"
mkdir -p "$BUNDLE/app"
tar -C "$APP_DIR" -cf - \
    --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='.ruff_cache' --exclude='.env' --exclude='*.pyc' \
    . | tar -C "$BUNDLE/app" -xf -

# ---------------------------------------------------------------- 依赖锁
# 从 uv.lock 导出精确版本(带 hash 的行给 --require-hashes 用, 这里用
# 无 hash 版配合 pip download; 安装时仍锁版本, 不会漂)。
log "导出依赖锁 (uv export)"
cd "$APP_DIR"
uv export --format requirements.txt --no-hashes --frozen --no-emit-project \
    -o "$BUNDLE/requirements.lock"
echo "锁定包数: $(grep -c '^[a-zA-Z]' "$BUNDLE/requirements.lock" || true)"

# ---------------------------------------------------------------- wheelhouse
log "下载 aarch64 wheelhouse (仅二进制, 之后安装全程 --no-index)"
# 在 /tmp 建 venv, **不要** cd $APP_DIR 再建: uv 在项目目录下会顺手去写
# uv.lock, 而 /opt/aterag 归 aterag、脚本有时以别的身份跑, 实测就是
# 「uv venv 报 Permission denied /opt/aterag/uv.lock」把整段打断。
# 依赖锁的导出(uv export)必须在项目目录里做, 那是只读操作, 没问题。
# --clear 是必需的: 上一轮跑失败留下的 /tmp/wheelenv 会让 uv 直接报错退出
# (「A virtual environment already exists」), 于是这一轮看起来在下载, 实际
# 一步没走 —— 而日志停在「下载」那行, 看上去像网络慢。
( cd /tmp && uv venv --seed --clear /tmp/wheelenv >/dev/null )
/tmp/wheelenv/bin/python -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
mkdir -p "$BUNDLE/wheelhouse"
# --only-binary :all: 是硬要求: 离线安装时现场编 C 扩展必然失败, 与其
# 装到一半炸, 不如这里就只收轮子。
XDG_CACHE_HOME=/tmp/wheelcache /tmp/wheelenv/bin/python -m pip download \
    --only-binary :all: --dest "$BUNDLE/wheelhouse" \
    -r "$BUNDLE/requirements.lock"
echo "轮子数: $(ls -1 "$BUNDLE/wheelhouse" | wc -l)"
rm -rf /tmp/wheelenv /tmp/wheelcache

# ---------------------------------------------------------------- 脚本
install_sh="$BUNDLE/app/scripts/offline/install.sh"
verify_sh="$BUNDLE/app/scripts/offline/verify.sh"
[ -f "$install_sh" ] || { echo "缺少 $install_sh" >&2; exit 1; }
[ -f "$verify_sh" ] || { echo "缺少 $verify_sh" >&2; exit 1; }
chmod +x "$install_sh" "$verify_sh" "$BUNDLE/app/scripts/offline/make_bundle.sh"
cp "$install_sh" "$BUNDLE/install.sh"
cp "$verify_sh" "$BUNDLE/verify.sh"
cp "$APP_DIR/scripts/offline/README.md" "$BUNDLE/README.md"

# ---------------------------------------------------------------- 清单
log "生成清单 (sha256 + 版本事实)"
/opt/aterag/.venv/bin/python "$BUNDLE/app/scripts/offline/manifest.py" \
    --root "$BUNDLE" \
    --git-sha "$GIT_SHA" \
    --out-manifest "$BUNDLE/MANIFEST.json" \
    --out-checksums "$BUNDLE/MANIFEST.sha256"

# ---------------------------------------------------------------- 打包
log "打包"
tar -C "$OUT_ROOT" -czf "$OUT_ROOT/aterag-offline-$STAMP-$GIT_SHA.tar.gz" \
    "aterag-offline-$STAMP-$GIT_SHA"
cd "$OUT_ROOT"
sha256sum "aterag-offline-$STAMP-$GIT_SHA.tar.gz" \
    > "aterag-offline-$STAMP-$GIT_SHA.tar.gz.sha256"
du -sh "$BUNDLE" "$OUT_ROOT/aterag-offline-$STAMP-$GIT_SHA.tar.gz"
echo "BUNDLE=$BUNDLE"