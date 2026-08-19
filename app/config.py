from __future__ import annotations

from functools import lru_cache
from typing import List, Optional, Tuple

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="GATEWAY_", extra="ignore")

    upstream_base_url: str = Field(
        default="http://localhost:9000",
        description="Base URL of the internal AI Agent / LLM infrastructure the gateway proxies to.",
    )
    upstream_timeout_seconds: float = Field(default=60.0, ge=1.0)
    upstream_connect_timeout_seconds: float = Field(default=5.0, ge=1.0)

    jwt_public_key: str = Field(
        default="",
        description="PEM-encoded public key used to verify JWTs issued by the enterprise IdP "
        "(e.g. Okta, Azure AD). For JWKS-based rotation, resolve the active key to this field "
        "at startup / on a refresh timer rather than hardcoding it.",
    )
    jwt_algorithm: str = Field(default="RS256")
    jwt_audience: Optional[str] = Field(default=None)
    jwt_issuer: Optional[str] = Field(default=None)
    jwt_leeway_seconds: int = Field(default=10, ge=0)

    permissions_file_path: str = Field(default="app/policy/permissions.yaml")

    guardrails_spacy_model: str = Field(
        default="en_core_web_sm",
        description="Lightweight spaCy pipeline used by Presidio's NLP engine for NER.",
    )
    guardrails_score_threshold: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description="Minimum Presidio confidence score required before an entity is masked.",
    )
    guardrails_entities_to_mask: Optional[Tuple[str, ...]] = Field(
        default=None,
        description="Override the default entity allowlist masked by the guardrails engine. "
        "Leave unset to use app.guardrails.engine.DEFAULT_ENTITIES_TO_MASK.",
    )

    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL backing the rate limiter and token accounting.",
    )
    redis_max_connections: int = Field(default=50, ge=1)
    rate_limit_window_seconds: int = Field(
        default=60,
        ge=1,
        description="Sliding window size, in seconds, for the loop detector / rate limiter.",
    )
    rate_limit_max_requests: int = Field(
        default=20,
        ge=1,
        description="Max requests a single user_id may make within the sliding window before a 429.",
    )
    token_encoding_name: str = Field(
        default="cl100k_base",
        description="tiktoken encoding used for token accounting; falls back to a character "
        "heuristic if the encoding can't be loaded (e.g. restricted network egress).",
    )

    semantic_cache_enabled: bool = Field(default=True)
    semantic_cache_path: str = Field(
        default="./data/qdrant_semantic_cache",
        description="Local embedded Qdrant storage directory. Use a real Qdrant server URL "
        "in app.cache.vector_store instead for horizontally-scaled deployments.",
    )
    semantic_cache_collection_name: str = Field(default="gateway_semantic_cache")
    semantic_cache_similarity_threshold: float = Field(
        default=0.92,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity required for a cache hit.",
    )
    embedding_model_name: str = Field(default="all-MiniLM-L6-v2")
    embedding_fallback_dimensions: int = Field(
        default=384,
        ge=8,
        description="Vector size used by the hashing fallback embedder when the real model is unreachable.",
    )

    langfuse_enabled: bool = Field(default=False)
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")
    langfuse_host: str = Field(default="http://localhost:3000")

    service_name: str = Field(default="ai-governance-gateway")
    hop_by_hop_headers: List[str] = Field(
        default_factory=lambda: [
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
            "host",
            "content-length",
            "authorization",  
        ]
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()