#!/bin/bash
# ATERag 板卡原生部署 Step 4: PG 配置 + 扩展初始化
set -euo pipefail
PGCONF=/etc/postgresql/17/main
sudo tee -a "$PGCONF/postgresql.conf" >/dev/null <<'EOF'

# ---- ATERag ----
shared_preload_libraries = 'age,pg_textsearch'
listen_addresses = '*'
max_connections = 100
shared_buffers = 1GB
work_mem = 32MB
maintenance_work_mem = 512MB
EOF
# 客户端认证: 允许局域网 (仅 192.168.5.0/24), scram-sha-256
sudo tee -a "$PGCONF/pg_hba.conf" >/dev/null <<'EOF'
hostssl all all 192.168.5.0/24 scram-sha-256
host    all all 192.168.5.0/24 scram-sha-256
EOF

sudo systemctl restart postgresql
sleep 3

# 口令由环境变量注入, 不写死在仓库:
#   PG_ADMIN_PASSWORD -> postgres 超级用户口令
#   PG_APP_PASSWORD   -> powerspec 业务账号口令
: "${PG_ADMIN_PASSWORD:?必须设置 PG_ADMIN_PASSWORD}"
: "${PG_APP_PASSWORD:?必须设置 PG_APP_PASSWORD}"

sudo -u postgres env PG_ADMIN_PASSWORD="$PG_ADMIN_PASSWORD" PG_APP_PASSWORD="$PG_APP_PASSWORD" \
  psql -v ON_ERROR_STOP=1 \
       -v admin_password="$PG_ADMIN_PASSWORD" \
       -v app_password="$PG_APP_PASSWORD" <<'EOF'
ALTER USER postgres PASSWORD :'admin_password';
DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'powerspec') THEN
      CREATE ROLE powerspec LOGIN PASSWORD :'app_password';
   END IF;
END $$;
SELECT 'CREATE DATABASE power_specs OWNER powerspec'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'power_specs')\gexec
EOF

sudo -u postgres psql -v ON_ERROR_STOP=1 -d power_specs <<'EOF'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgvector;
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
CREATE EXTENSION IF NOT EXISTS zhparser;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('power_specs');
CREATE TEXT SEARCH CONFIGURATION public.chinese (PARSER = zhparser);
ALTER TEXT SEARCH CONFIGURATION public.chinese
    ADD MAPPING FOR n, v, a, i, e, l WITH simple;
GRANT ALL ON SCHEMA ag_catalog TO powerspec;
EOF

echo "== STEP4 DONE =="
sudo -u postgres psql -d power_specs -c "SELECT extname FROM pg_extension ORDER BY extname;"
