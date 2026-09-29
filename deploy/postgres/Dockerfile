# ATERag PostgreSQL 17 自定义镜像 (ARM64)
# 基底: pgvector/pgvector:pg17 (官方多架构镜像, 基于 postgres:17 官方镜像, 已含 pgvector)
# 叠加: Apache AGE (PG17 分支) + pg_textsearch v1.4.0 (官方 arm64 预编译) + zhparser(SCWS)
FROM docker.io/pgvector/pgvector:pg17

ARG PG_TEXTSEARCH_VERSION=1.4.0
ARG AGE_BRANCH=PG17
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates curl unzip bison flex \
        libcurl4-openssl-dev libicu-dev libxml2-dev \
        postgresql-server-dev-17 wget \
    && rm -rf /var/lib/apt/lists/*

# ---- Apache AGE (PG17 分支) ----
# 依赖: 必须先 LOAD 于 shared_preload_libraries
RUN git clone --depth 1 --branch release/PG17/1.7.0 https://github.com/apache/age.git /tmp/age \
    && cd /tmp/age \
    && make USE_PGXS=1 -j"$(nproc)" \
    && make USE_PGXS=1 install \
    && rm -rf /tmp/age

# ---- pg_textsearch v1.4.0 (Timescale 官方 arm64/PG17 预编译) ----
RUN curl -fsSL -o /tmp/pgts.zip \
        "https://github.com/timescale/pg_textsearch/releases/download/v${PG_TEXTSEARCH_VERSION}/pg-textsearch-v${PG_TEXTSEARCH_VERSION}-pg17-arm64.zip" \
    && cd /tmp && unzip -o pgts.zip \
    && cp -r pg-textsearch-v${PG_TEXTSEARCH_VERSION}-pg17-arm64/* /usr/local/ 2>/dev/null || true \
    && cd /tmp/pg-textsearch-v${PG_TEXTSEARCH_VERSION}-pg17-arm64 \
    && find . -name '*.so'   -exec cp {} /usr/local/lib/postgresql/ \; \
    && find . -name '*.control' -exec cp {} /usr/local/share/postgresql/extension/ \; \
    && find . -name '*.sql'  -exec cp {} /usr/local/share/postgresql/extension/ \; \
    && rm -rf /tmp/pgts.zip /tmp/pg-textsearch-*

# ---- SCWS (zhparser 依赖) ----
RUN git clone --depth 1 https://github.com/hightman/scws.git /tmp/scws \
    && cd /tmp/scws \
    && ./configure --prefix=/usr/local --disable-static \
    && make -j"$(nproc)" && make install \
    && ldconfig \
    && rm -rf /tmp/scws

# ---- zhparser (中文分词) ----
RUN git clone --depth 1 https://github.com/amutu/zhparser.git /tmp/zhparser \
    && cd /tmp/zhparser \
    && make USE_PGXS=1 -j"$(nproc)" \
    && make USE_PGXS=1 install \
    && rm -rf /tmp/zhparser

# ---- 配置: shared_preload_libraries 需要 age 与 pg_textsearch ----
# (initdb SQL 中再 SET search_path 与建图)
RUN echo "shared_preload_libraries = 'age, pg_textsearch'" \
      >> /usr/local/share/postgresql/postgresql.conf.sample \
    && echo "pg_textsearch.memory_limit = '256MB'" \
      >> /usr/local/share/postgresql/postgresql.conf.sample

COPY initdb/ /docker-entrypoint-initdb.d/
