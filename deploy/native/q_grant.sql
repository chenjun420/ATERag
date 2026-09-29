-- 检查 workspace 图的 base 表权限 (LIGHT-RAG merge 阶段失败疑因权限)
SELECT grantee, privilege_type FROM information_schema.role_table_grants
WHERE table_schema='PA601_D54A_chunk_entity_relation' LIMIT 12;
SELECT count(*) AS base_nodes FROM PA601_D54A_chunk_entity_relation.base;
