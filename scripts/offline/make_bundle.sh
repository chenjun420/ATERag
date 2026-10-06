#!/usr/bin/env bash
# 在板卡上组装离线版本包。
#
# 为什么在板卡上组装而不是在开发机: wheelhouse 必须是 **aarch64** 的轮子。
# 开发机是 Windows x86_64, 交叉编译带 C/Rust 扩展的轮子(psycopg-c、
# pydantic-core)不现实; 板卡本身就是 aarch64, 在这里下载到的就是目标
# 平台的轮子。
#
# 产出的包是**可重放**的: 同一 commit + 同一依赖锁 -> 同一份包,
# 且安装过程不触网(--no-index)。
#
# ---------------------------------------------------------------- 换源
# 这一版把依赖锁改成**带 sha256** 导出、下载走 ``--require-hashes``,
# 并允许换用国内镜像。理由与代价都是实测出来的(2026-10-06, 板卡):
#
#   * 直连 files.pythonhosted.org: 索引页 ~2.3 MB/s, 但**轮子文件本身只有
#     7~23 kB/s** —— 索引快、文件慢是这个链路的实况。227 MB 的 wheelhouse
#     跑了两个多小时没下完, 而且日志停在「Downloading <当前包>」那一行,
#     看上去像网络慢, 实际是 17 kB/s 的龟速。
#   * 清华 tuna 镜像托管的是文件本身: 实测 20~29 MB/s, 227 MB / 116 个
#     轮子 **90 秒**下完。
#   * 阿里云镜像也快(ortools 27.6 MB 实测 12 MB/s), 但**缺** psycopg-binary
#     与 scikit-learn 的 aarch64 轮子 —— 镜像的覆盖度不是想当然的, 所以
#     不进默认列表。真要用就自己加进 PYPI_INDEX, 缺轮子时 pip 会报出来。
#
# 换源为什么不等于把完整性交给镜像: requirements.lock 里的 sha256 来自
# **uv.lock**(仓库里已审过的锁), ``--require-hashes`` 让 pip 逐个校验
# 下载到的文件。实测把一个已下载的轮子尾部追加 15 字节再让它装, pip 报
# ``THESE PACKAGES DO NOT MATCH THE HASHES`` 并以 rc=1 退出。镜像只提供
# 带宽, 内容对不对由仓库的锁说了算。
#
# 因此 PIP_INDEX 是可覆盖的环境变量而不是写死的常量: 换网络环境时不必改
# 代码, 而无论用哪个源, 完整性判据都不变。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aterag}"
OUT_ROOT="${OUT_ROOT:-/opt/aterag-bundle}"
GIT_SHA="${1:?用法: make_bundle.sh <git-sha>}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE="$OUT_ROOT/aterag-offline-$STAMP-$GIT_SHA"

# 依次尝试; 第一个成功的即为本次实际取包源(记进包里, 见 INDEX_USED)。
# tuna 排第一是因为它在本板卡上实测全速且覆盖齐备, pypi.org 兜底。
PYPI_INDEX="${PYPI_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PYPI_INDEX_FALLBACK="${PYPI_INDEX_FALLBACK:-https://pypi.org/simple}"

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
# **必须带 sha256**: 这是换用镜像的唯一安全前提(见文件头)。不带 hash 的
# 锁一旦从第三方源取包, 包内容就只由那个源担保 —— 那不是校验, 是信任。
log "导出依赖锁 (uv export, 带 sha256)"
cd "$APP_DIR"
uv export --format requirements.txt --frozen --no-emit-project \
    -o "$BUNDLE/requirements.lock"
echo "锁定包数: $(grep -c '^[a-zA-Z]' "$BUNDLE/requirements.lock" || true)"
echo "hash 行数: $(grep -c -- '--hash=sha256:' "$BUNDLE/requirements.lock" || true)"
# 带 hash 是硬要求, 不是优化: 少了它下面 --require-hashes 会直接失败,
# 那正是我们要的「响亮地坏掉」而不是悄悄降级成无校验下载。
grep -q -- '--hash=sha256:' "$BUNDLE/requirements.lock" \
    || { echo "依赖锁里没有 sha256, 拒绝组装 —— 换源下载将失去完整性保证" >&2; exit 1; }

# ---------------------------------------------------------------- 拿 pip
# 优先用现成的 pip, 别为此新建带 seed 的 venv: seed 包自己要联网下, 而
# 这一步失败会表现成「日志停在下载那行, 看上去像网络慢」。
log "准备下载环境"
PIP_PY=""
for cand in "$APP_DIR/.venv/bin/python" /usr/bin/python3; do
    if [ -x "$cand" ] && "$cand" -m pip --version >/dev/null 2>&1; then
        PIP_PY="$cand"
        break
    fi
done
if [ -z "$PIP_PY" ]; then
    # 兜底: 系统没 pip 时借 uv 造一个。--clear 是必需的 —— 上一轮跑失败
    # 留下的 /tmp 目录会让 uv 直接报错退出, 于是这一轮看起来在下载、实际
    # 一步没走。venv 必须建在 /tmp: uv 在项目目录下会顺手去写 uv.lock,
    # 而 /opt/aterag 归 aterag, 实测就是「Permission denied /opt/aterag/uv.lock」。
    ( cd /tmp && uv venv --seed --clear /tmp/bundle-wheelenv >/dev/null )
    /tmp/bundle-wheelenv/bin/python -m pip --version >/dev/null 2>&1 \
        || { echo "造不出可用的 pip" >&2; exit 1; }
    PIP_PY=/tmp/bundle-wheelenv/bin/python
fi
echo "pip: $("$PIP_PY" -m pip --version)"

# ---------------------------------------------------------------- wheelhouse
log "下载 aarch64 wheelhouse (仅二进制 + 强制 hash 校验, 装时全程 --no-index)"
WHEELHOUSE="$BUNDLE/wheelhouse"
rm -rf "$WHEELHOUSE"
mkdir -p "$WHEELHOUSE"

# --only-binary :all: 是硬要求: 离线安装时现场编 C 扩展必然失败, 与其
# 装到一半炸, 不如这里就只收轮子。
# --require-hashes: 逐个按 uv.lock 的 sha256 校验下载到的文件。
# XDG_CACHE_HOME 固定到 /tmp: 缓存目录若跟着 $HOME 走, 而脚本有时以别的
# 身份跑, 会在「看起来像网络慢」的地方先撞上权限错误。
export XDG_CACHE_HOME=/tmp/bundle-wheelcache

download_from() {
    local idx="$1"
    echo "--- 取包源: $idx"
    local t0 t1
    t0=$(date +%s)
    "$PIP_PY" -m pip download --require-hashes --only-binary :all: \
        --dest "$WHEELHOUSE" --index-url "$idx" \
        -r "$BUNDLE/requirements.lock"
    t1=$(date +%s)
    echo "--- 用时 $((t1 - t0))s, 轮子 $(ls -1 "$WHEELHOUSE" | wc -l) 个"
}

USED_INDEX=""
if download_from "$PYPI_INDEX"; then
    USED_INDEX="$PYPI_INDEX"
elif [ -n "$PYPI_INDEX_FALLBACK" ] && [ "$PYPI_INDEX_FALLBACK" != "$PYPI_INDEX" ] \
     && download_from "$PYPI_INDEX_FALLBACK"; then
    USED_INDEX="$PYPI_INDEX_FALLBACK"
else
    echo "所有取包源都失败" >&2
    exit 1
fi

# 轮子数必须对上锁定包数减去「本平台不适用」的那些(环境标记筛掉的,
# 例如只在 win32 或 py3.12 生效的条目)。对不上就说明少东西了, 而
# 「少一个轮子」的表现是装到一半才炸 —— 那正是要在这一步拦住的事。
locked=$(grep -c '^[a-zA-Z]' "$BUNDLE/requirements.lock" || true)
got=$(ls -1 "$WHEELHOUSE" | wc -l)
echo "锁定 $locked / 实得 $got 个轮子 (取包源 $USED_INDEX)"
if [ "$got" -eq 0 ]; then
    echo "一个轮子都没拿到, 拒绝继续 —— 后面会打出一个装不上的包" >&2
    exit 1
fi

# 取包源写进包里: 清单只保证文件没被改, 不保证「当初是从哪取的」。
# 出问题时这是第一个要看的线索, 不该靠回忆构建命令来还原。
printf '%s\n' "$USED_INDEX" > "$BUNDLE/INDEX_USED"

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
"$PIP_PY" "$BUNDLE/app/scripts/offline/manifest.py" \
    --root "$BUNDLE" \
    --git-sha "$GIT_SHA" \
    --index-url "$USED_INDEX" \
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