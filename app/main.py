from __future__ import annotations

import json as _json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.auth.jwt_utils import UserContext
from app.auth.middleware import JWTAuthMiddleware
from app.cache.embeddings import EmbeddingEngine
from app.cache.semantic_cache import SemanticCache, extract_cache_query_text
from app.cache.vector_store import QdrantVectorStore
from app.config import get_settings
from app.documents.pipeline import generate_request_token, payload_references_documents, resolve_document_references
from app.documents.registry import DocumentRegistry
from app.guardrails.engine import get_engine, init_guardrails_engine
from app.guardrails.pipeline import build_persisted_view, mask_inbound_payload, mask_outbound_response_json
from app.observability.langfuse_logger import AuditLogger
from app.policy.enforcement import (
    PolicyLoadError,
    enforce_policy_on_payload,
    get_masking_exempt_entities,
    get_permission_matrix,
)
from app.proxy.client import UpstreamProxyClient, UpstreamUnavailableError
from app.rate_limit.limiter import SlidingWindowRateLimiter
from app.rate_limit.redis_client import RedisClientManager
from app.rate_limit.token_accounting import TokenAccountingEngine, TokenCounter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()
proxy_client = UpstreamProxyClient(settings)
redis_manager = RedisClientManager(settings)
audit_logger = AuditLogger(settings)

document_registry = DocumentRegistry()

rate_limiter: Optional[SlidingWindowRateLimiter] = None
token_accounting: Optional[TokenAccountingEngine] = None
semantic_cache: Optional[SemanticCache] = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global rate_limiter, token_accounting, semantic_cache

    try:
        get_permission_matrix(settings.permissions_file_path)
        logger.info("Permission matrix loaded successfully from %s", settings.permissions_file_path)
    except PolicyLoadError:
        logger.exception("Permission matrix failed to load at startup.")
        raise

    init_guardrails_engine(settings)
    logger.info("Guardrails engine ready.")

    await redis_manager.start()
    rate_limiter = SlidingWindowRateLimiter(
        redis_manager.client,
        window_seconds=settings.rate_limit_window_seconds,
        max_requests=settings.rate_limit_max_requests,
    )
    token_accounting = TokenAccountingEngine(redis_manager.client, TokenCounter(settings.token_encoding_name))
    logger.info(
        "Rate limiter ready (limit=%d requests / %ds window).",
        settings.rate_limit_max_requests,
        settings.rate_limit_window_seconds,
    )

    if settings.semantic_cache_enabled:
        embeddings = EmbeddingEngine(
            model_name=settings.embedding_model_name,
            fallback_dimensions=settings.embedding_fallback_dimensions,
        )
        vector_store = QdrantVectorStore(
            path=settings.semantic_cache_path,
            collection_name=settings.semantic_cache_collection_name,
            dimensions=embeddings.dimensions,
        )
        semantic_cache = SemanticCache(
            embeddings,
            vector_store,
            similarity_threshold=settings.semantic_cache_similarity_threshold,
        )
        logger.info(
            "Semantic cache ready (semantic_embeddings=%s, threshold=%.2f).",
            embeddings.is_semantic,
            settings.semantic_cache_similarity_threshold,
        )
    else:
        logger.info("Semantic cache disabled (semantic_cache_enabled=False).")

    await proxy_client.start()
    yield
    await proxy_client.stop()
    await redis_manager.stop()
    audit_logger.shutdown()


app = FastAPI(title="AI Governance Gateway", version="0.1.0", lifespan=lifespan)
app.add_middleware(JWTAuthMiddleware, settings=settings)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.service_name}


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def governed_proxy(path: str, request: Request) -> Response:
    """Catch-all reverse proxy: intercepts every OpenAI-compatible / AI-Agent
    request, enforces governance policy against the caller's clearance
    level, then forwards the (mutated) payload to the internal AI Agent
    infrastructure.
    """
    user: Optional[UserContext] = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Missing authenticated user context.")

    assert rate_limiter is not None  
    limit_result = await rate_limiter.check_and_record(user.user_id)
    if not limit_result.allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded: {limit_result.request_count} requests in "
                f"{limit_result.window_seconds}s (limit={limit_result.limit}). "
                f"Retry after {limit_result.retry_after_seconds}s."
            ),
        )

    json_body: Optional[dict] = None
    policy_filtered_body: Optional[dict] = None
    raw_body = await request.body()
    if raw_body:
        try:
            json_body = _json.loads(raw_body)
        except _json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Request body is not valid JSON: {exc}") from exc

    document_token = generate_request_token()
    has_document_reference = False

    if json_body is not None:
        try:
            policy_filtered_body = enforce_policy_on_payload(json_body, user, settings)
        except Exception as exc:  # noqa: BLE001 -> never lets a policy bug leak
            logger.exception("Policy enforcement failed for user_id=%s path=%s", user.user_id, path)
            raise HTTPException(status_code=500, detail="Governance policy enforcement failed.") from exc

        has_document_reference = payload_references_documents(policy_filtered_body)
        if has_document_reference:
            try:
                policy_filtered_body, _ = resolve_document_references(
                    policy_filtered_body, user, document_registry, document_token
                )
            except Exception as exc:  # noqa: BLE001 -> blocks the call, does not leak a document
                logger.exception("Document reference resolution failed for user_id=%s path=%s", user.user_id, path)
                raise HTTPException(status_code=500, detail="Document access resolution failed.") from exc

        try:
            json_body = mask_inbound_payload(
                policy_filtered_body, get_engine(), exempt_entities=get_masking_exempt_entities(user, settings)
            )
        except Exception as exc:  # noqa: BLE001 -> doesn't allow a masking bug to leak PII
            logger.exception("Inbound data masking failed for user_id=%s path=%s", user.user_id, path)
            raise HTTPException(status_code=500, detail="Governance data-masking enforcement failed.") from exc

    assert token_accounting is not None  
    prompt_tokens = token_accounting.count_prompt_tokens(json_body)

    persisted_request_body: Optional[dict] = None
    if policy_filtered_body is not None:
        try:
            persisted_request_body = build_persisted_view(
                policy_filtered_body, get_engine(), document_token=document_token
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Persisted-view masking failed for user_id=%s path=%s; skipping cache/audit-log for this request.",
                user.user_id,
                path,
            )
            persisted_request_body = None

    cache_query_text = ""
    has_tools = isinstance(json_body, dict) and bool(json_body.get("tools"))
    if semantic_cache is not None and persisted_request_body is not None and not has_tools and not has_document_reference:
        cache_query_text = extract_cache_query_text(persisted_request_body)
        try:
            lookup_result = semantic_cache.lookup(cache_query_text)
        except Exception:  # noqa: BLE001 -> a cache bug should degrades to a miss
            logger.exception("Semantic cache lookup failed for user_id=%s path=%s; treating as a miss.", user.user_id, path)
            lookup_result = None

        if lookup_result is not None and lookup_result.hit and lookup_result.response_payload is not None:
            audit_logger.log_call(
                user_id=user.user_id,
                department=user.department,
                masked_request=persisted_request_body,
                masked_response=lookup_result.response_payload,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                cache_hit=True,
            )
            return Response(
                content=_json.dumps(lookup_result.response_payload).encode("utf-8"),
                status_code=200,
                media_type="application/json",
                headers={"X-Cache": "HIT"},
            )

    try:
        upstream_response = await proxy_client.forward(request, path, json_body)
    except UpstreamUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    response_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in {"content-encoding", "transfer-encoding", "content-length", "connection"}
    }

    content_type = upstream_response.headers.get("content-type", "")
    response_body = upstream_response.content
    completion_tokens = 0
    persisted_response_body: Optional[dict] = None

    if "application/json" in content_type and response_body:
        try:
            response_json = _json.loads(response_body)
        except _json.JSONDecodeError:
            logger.error(
                "Upstream response for user_id=%s path=%s claimed JSON but failed to parse; withholding it.",
                user.user_id,
                path,
            )
            raise HTTPException(
                status_code=502, detail="Upstream returned malformed JSON; response withheld for safety."
            )

        raw_response_json = response_json 

        try:
            response_json = mask_outbound_response_json(
                raw_response_json, get_engine(), exempt_entities=get_masking_exempt_entities(user, settings)
            )
        except Exception as exc:  # noqa: BLE001 -> same fail-closed reasoning as above
            logger.exception("Outbound data masking failed for user_id=%s path=%s", user.user_id, path)
            raise HTTPException(status_code=500, detail="Governance data-masking enforcement failed.") from exc

        if has_document_reference:
            persisted_response_body = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "[RESPONSE OMITTED FROM AUDIT LOG: generated using a restricted document]",
                        }
                    }
                ]
            }
        else:
            try:
                persisted_response_body = build_persisted_view(raw_response_json, get_engine(), is_response=True)
            except Exception:  # noqa: BLE001 -> fail closed for persistence only
                logger.exception(
                    "Persisted-view masking failed for user_id=%s path=%s; skipping cache/audit-log for this response.",
                    user.user_id,
                    path,
                )
                persisted_response_body = None

        completion_tokens = token_accounting.count_completion_tokens(response_json)
        response_body = _json.dumps(response_json).encode("utf-8")

    try:
        await token_accounting.record_usage(user.department, prompt_tokens, completion_tokens)
    except Exception:  # noqa: BLE001 -> accounting shouldn't block a response from returning
        logger.exception(
            "Token accounting failed for user_id=%s department=%s path=%s", user.user_id, user.department, path
        )

    if (
        semantic_cache is not None
        and cache_query_text
        and not has_document_reference
        and persisted_response_body is not None
    ):
        try:
            semantic_cache.store(cache_query_text, persisted_response_body)
        except Exception:  # noqa: BLE001 
            logger.exception("Semantic cache store failed for user_id=%s path=%s", user.user_id, path)

    audit_logger.log_call(
        user_id=user.user_id,
        department=user.department,
        masked_request=persisted_request_body,
        masked_response=persisted_response_body,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit=False,
    )

    return Response(
        content=response_body,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": "request_failed", "detail": exc.detail})