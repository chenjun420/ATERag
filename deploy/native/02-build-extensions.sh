#!/bin/bash
# ATERag 板卡原生部署 Step 2: 编译扩展 (AGE / SCWS+zhparser / pg_textsearch)
# PGDG 布局: PG17 位于 /usr/lib/postgresql/17
set -euo pipefail
PGBIN=/usr/lib/postgresql/17/bin
MAKEJ=$(nproc)
cd /tmp

# ---- Apache AGE (PG17 分支) ----
if ! ls "$PGBIN"/.. 2>/dev/null | grep -q age && ! test -f "$(pg_config --pkglibdir 2>/dev/null || echo /usr/lib/postgresql/17/lib)/age.so"; then
  git clone --depth 1 --branch release/PG17/1.7.0 https://github.com/apache/age.git
  cd age && make USE_PGXS=1 PG_CONFIG="$PGBIN/pg_config" -j"$MAKEJ" && \
      sudo make USE_PGXS=1 PG_CONFIG="$PGBIN/pg_config" install && cd /tmp || exit 1
  rm -rf /tmp/age
  echo "== AGE installed =="
fi

# ---- SCWS (zhparser 依赖) ----
git clone --depth 1 https://github.com/hightman/scws.git
cd scws && ./configure --prefix=/usr/local --disable-static >/dev/null && \
    make -j"$MAKEJ" && sudo make install && sudo ldconfig && cd /tmp || exit 1
rm -rf /tmp/scws
echo "== SCWS installed =="

# ---- zhparser ----
git clone --depth 1 https://github.com/amutu/zhparser.git
cd zhparser && make PG_CONFIG="$PGBIN/pg_config" -j"$MAKEJ" && \
    sudo make PG_CONFIG="$PGBIN/pg_config" install && cd /tmp || exit 1
rm -rf /tmp/zhparser
echo "== zhparser installed =="

# ---- pg_textsearch v1.4.0 (源码编译) ----
# 不做「失败降级 tsvector」: pg_textsearch 是 BM25 检索的唯一来源
# (REQUIRED_EXTENSIONS), 缺它时 storage/schema.py 的
# assert_extensions_installed 直接抛 SchemaError。脚本在这里降级成功, 只会把
# 失败推迟到服务启动那一刻, 且日志里看不出是这一步没装上。
curl -fsSL -o pgt.tar.gz \
    https://github.com/timescale/pg_textsearch/releases/download/v1.4.0/pg_textsearch-1.4.0.tar.gz
tar xzf pgt.tar.gz
cd pg_textsearch-1.4.0
make PG_CONFIG="$PGBIN/pg_config" -j"$MAKEJ"
sudo make PG_CONFIG="$PGBIN/pg_config" install
echo "== pg_textsearch installed =="
cd /tmp && rm -rf pgt.tar.gz pg_textsearch-1.4.0

echo "== STEP2 DONE =="
ls /usr/lib/postgresql/17/lib/ | grep -E "age|zhparser|pg_textsearch|vector" || true
ls /usr/share/postgresql/17/extension/ | grep -E "age|zhparser|pg_textsearch|vector" || true
