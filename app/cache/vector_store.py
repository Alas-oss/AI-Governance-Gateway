from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, ScoredPoint, VectorParams

logger = logging.getLogger(__name__)

class QdrantVectorStore:

    def __init__(self, *, path: str, collection_name: str, dimensions: int) -> None:
        self._client = QdrantClient(path=path)
        self._collection_name = collection_name
        self._ensure_collection(dimensions)

    def _ensure_collection(self, dimensions: int) -> None:
        existing = {c.name for c in self._client.get_collections().collections}
        if self._collection_name not in existing:
            self._client.create_collection(
                collection_name=self._collection_name,
                vector_config=VectorParams(size=dimensions, distance=Distance.COSINE),
            )
            logger.info(
                "Created semantic cache collection '%s' (dimensions=%d).", self._collection_name, dimensions
            )

    def search(self, vector: List[float], *, limit: int = 1) -> List[ScoredPoint]:
        result = self._client.query_points(
            collection_name=self._collection_name,
            query=vector,
            limit=limit,
            with_payload=True,
        )
        return result.points

    def upsert(self, vector: List[float], payload: Dict[str, Any]) -> str:
        point_id = str(uuid.uuid4())
        self._client.upsert(
            collection_name=self._collection_name,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        return point_id