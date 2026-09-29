-- ATERag 初始化: 扩展加载 + 图数据库 + 中文 BM25 配置
-- 由 docker-entrypoint-initdb.d 在首次建库时自动执行

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
CREATE EXTENSION IF NOT EXISTS zhparser;

-- Apache AGE: LOAD + search_path (每次会话需要, 以下仅初始化验证)
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('power_specs');

-- 中文文本搜索配置 (zhparser 分词, 实词映射 simple)
CREATE TEXT SEARCH CONFIGURATION public.chinese (PARSER = zhparser);
ALTER TEXT SEARCH CONFIGURATION public.chinese
    ADD MAPPING FOR n, v, a, i, e, l WITH simple;

-- 权限对齐: 业务账号可用 (与 POSTGRES_USER 相同, 一般无需额外授权)
