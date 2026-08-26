from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.cache.embeddings import EmbeddingEngine
from app.cache.vector_store import QdrantVectorStore

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class CacheLookupResult:
    hit: bool
    response_payload: Optional[Dict[str, Any]] = None
    similarity_score: Optional[float] = None

def extract_cache_query_text(payload: Optional[Dict[str, Any]]) -> str:
    if not isinstance(payload, dict):
        return ""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""

class SemanticCache:

    def __init__(self, embeddings: EmbeddingEngine, store: QdrantVectorStore, *, similarity_threshold: float) -> None:
        self._embeddings = embeddings
        self._store = store
        self._similarity_threshold = similarity_threshold

    def lookup(self, query_text: str) -> CacheLookupResult:
        if not query_text:
            return CacheLookupResult(hit=False)

        vector = self._embeddings.embed(query_text)
        points = self._store.search(vector, limit=1)
        if not points:
            return CacheLookupResult(hit=False)

        best = points[0]
        if best.score < self._similarity_threshold:
            logger.info(
                "Semantic cache miss: best match scored %.3f, below threshold %.3f.",
                best.score, 
                self._similarity_threshold,
            )
            return CacheLookupResult(hit=False, similarity_score=best.score)

        logger.info("Semantic cache HIT (score=%.3f, threshold=%.3f).", best.score, self._similarity_threshold)
        response_payload = best.payload.get("response") if best.payload else None
        return CacheLookupResult(hit=True, response_payload=response_payload, similarity_score=best.score)

    def store(self, query_text: str, response_payload: Dict[str, Any]) -> None:
        if not query_text:
            return
        vector = self._embeddings.embed(query_text)
        self._store.upsert(vector, payload={"query_text": query_text, "response": response_payload})