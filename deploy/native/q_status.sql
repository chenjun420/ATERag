SELECT id, status, error_msg IS NOT NULL AS has_error, left(coalesce(error_msg,''), 120) AS err FROM lightrag_doc_status;
