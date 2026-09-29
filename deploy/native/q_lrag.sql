SELECT count(*) AS entity_chunks FROM lightrag_entity_chunks;
SELECT relname FROM pg_tables WHERE tablename LIKE 'lightrag%' ORDER BY 1;
SELECT count(*) AS lrag_vdb FROM lightrag_vdb_entity;
