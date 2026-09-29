#!/bin/bash
# ATERag 板卡原生部署 Step 1: PGDG 仓库 + PostgreSQL 17 + pgvector + 编译依赖
# 目标: Debian 12 ARM64 (192.168.5.24), 内核高度裁剪, 必须原生安装 (禁容器)
set -euo pipefail

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg lsb-release

# PGDG 官方仓库 (含 postgresql-17, postgresql-17-pgvector, server-dev-17)
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
    | sudo tee /etc/apt/sources.list.d/pgdg.list

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    postgresql-17 postgresql-client-17 postgresql-server-dev-17 \
    postgresql-17-pgvector \
    build-essential git bison flex pkg-config wget unzip

echo "== STEP1 DONE =="
/usr/lib/postgresql/17/bin/pg_config --version
