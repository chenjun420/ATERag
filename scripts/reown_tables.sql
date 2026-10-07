-- 把 pw_* / l0_term 的表与序列属主改成应用账号, 让 alembic 能自助跑。
--
-- 为什么需要: 表属主是 postgres, 应用账号 powerspec 只有 DML 权限 —— alembic 的
-- ALTER TABLE / CREATE INDEX / COMMENT ON 全部要属主或超级用户, 于是 0004 就已经
-- 卡住, 迁移链断在那里。属主改过来之后, 后续 DDL 变更不必每次找人拿超管口令。
--
-- 幂等: 可重复执行。只改 l0_term.* 与 pw_*.* 的表/序列, 不动 schema 本身与角色授权。
--
-- 用法(在有 superuser 口令的环境里):
--   psql -h 192.168.5.25 -U postgres -d power_specs -v ON_ERROR_STOP=1 -f scripts/reown_tables.sql
--   换目标角色: 追加 -v target_role=<角色名>
--
-- 验证(应用账号自足, 不再需要超管):
--   .venv\Scripts\python.exe -m alembic upgrade head

\set ON_ERROR_STOP on
\if :{?target_role}
\else
  \set target_role powerspec
\endif

\echo '目标角色:' :target_role

-- 用 \gexec 而不是 DO $$ ... $$: psql 变量在 dollar-quoted 体内不会被替换,
-- 而这里必须把 target_role 传进 SQL。\gexec 逐行执行 SELECT 返回的语句。

SELECT format('ALTER TABLE %I.%I OWNER TO %I', n.nspname, c.relname, :'target_role')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND (n.nspname LIKE 'pw\_%' OR n.nspname = 'l0_term')
  AND pg_get_userbyid(c.relowner) <> :'target_role'
\gexec

-- 序列一并处理: 序列属主不对会让 nextval 的权限判断出错(默认权限按属主走)。
SELECT format('ALTER SEQUENCE %I.%I OWNER TO %I', n.nspname, c.relname, :'target_role')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'S'
  AND (n.nspname LIKE 'pw\_%' OR n.nspname = 'l0_term')
  AND pg_get_userbyid(c.relowner) <> :'target_role'
\gexec

\echo '剩余非目标角色属主的表(应为空):'
SELECT n.nspname, c.relname, pg_get_userbyid(c.relowner)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','S')
  AND (n.nspname LIKE 'pw\_%' OR n.nspname = 'l0_term')
  AND pg_get_userbyid(c.relowner) <> :'target_role';