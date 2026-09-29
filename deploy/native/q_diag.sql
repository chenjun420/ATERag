-- 诊断: PG 重复行 + 4.3.3 组块内容
SELECT count(*) AS total, count(DISTINCT content) AS distinct_content FROM aterag_chunks WHERE workspace_id = 'PA601-D54A';
SELECT id, req_id, rail, priority, left(content, 80) FROM aterag_chunks WHERE workspace_id='PA601-D54A' AND section_path='4.3.3' ORDER BY id;
