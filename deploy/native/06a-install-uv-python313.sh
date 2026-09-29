#!/bin/bash
# 板卡侧: 安装 uv 并拉取预编译 CPython 3.13 (不走源码编译)
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends python3.11-venv ca-certificates curl git build-essential

export PATH="$HOME/.local/bin:$PATH"
curl -sSL --max-time 120 https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
sh /tmp/uv-install.sh
uv --version

echo "===== 拉取预编译 CPython 3.13 ====="
uv python install 3.13
uv python list | head -10
