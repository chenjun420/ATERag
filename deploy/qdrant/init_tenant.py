#!/usr/bin/env python3
"""Qdrant 初始化: 创建 lightrag_vectors collection + workspace_id tenant 索引.

用法 (部署后, 在开发机执行): python deploy/qdrant/init_tenant.py
依赖: pip install qdrant-client
"""
from __future__ import annotations

import os
import sys

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    KeywordIndexParams,
    PayloadSchemaType,
    VectorParams,
)

COLLECTION = os.getenv("QDRANT_COLLECTION", "lightrag_vectors")
# 维度自动探测原则: 首次真实 embedding 后可调用 update_collection 调整
VECTOR_SIZE = int(os.getenv("QDRANT_VECTOR_SIZE", "2560"))
WORKSPACE_FIELD = "workspace_id"


def main() -> int:
    url = os.getenv("QDRANT_URL", "http://192.168.5.25:6333")
    client = QdrantClient(url=url, timeout=30)

    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION not in existing:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"created collection {COLLECTION} (size={VECTOR_SIZE})")
    else:
        print(f"collection {COLLECTION} exists, skip create")

    idx = client.get_collection(COLLECTION).payload_schema or {}
    if WORKSPACE_FIELD not in idx:
        client.create_payload_index(
            collection_name=COLLECTION,
            field_name=WORKSPACE_FIELD,
            field_schema=KeywordIndexParams(
                type=PayloadSchemaType.KEYWORD,
                is_tenant=True,
            ),
        )
        print(f"created tenant index {WORKSPACE_FIELD} (is_tenant=True)")
    else:
        print(f"tenant index {WORKSPACE_FIELD} exists, skip")

    info = client.get_collection(COLLECTION)
    print(f"OK points={info.points_count} status={info.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
