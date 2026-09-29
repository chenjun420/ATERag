-- ATERag 扩展初始化 (修正版: 扩展名为 vector, 无 pgvector)
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
GRANT SELECT ON ag_catalog.ag_graph TO powerspec;
GRANT SELECT ON ag_catalog.ag_label TO powerspec;
ALTER DEFAULT PRIVILEGES IN SCHEMA ag_catalog GRANT ALL ON TABLES TO powerspec;
SELECT extname FROM pg_extension ORDER BY extname;
