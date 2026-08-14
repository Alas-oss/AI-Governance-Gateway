from __future__ import annotations

import json as _json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.auth.jwt_utils import UserContext
from app.auth.middleware import JWTAuthMiddleware
from app.config import get_settings
from app.policy.enforcement import PolicyLoadError, enforce_policy_on_payload
from app.proxy.client import UpstreamProxyClient, UpstreamUnavailableError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()
proxy_client = UpstreamProxyClient(settings)

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        get_permission_matrix(settings.permissions_file_path)
        logger.info("Permission matrix loaded successgully from %s", settings.permissions_file_path)
    except PolicyLoadError:
        logger.exception("Permission matrix failed to load at startup.")
        raise

    await proxy_client.start()
    yield
    await proxy_client.stop()

app = FastAPI(title="AI Governance Gateway", version="0.1.0", lifespan=lifespan)
app.add_middleware(JWTAuthMiddleware, settings=settings)

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.service_name}

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def geverned_proxy(path: str, request: Request) -> Response:
    user: Optional[UserContext] = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Missing authenticated user context.")

    json_body: Optional[dict] = None
    raw_body = await request.body()
    if raw_body:
        try:
            json_body = _json.loads(raw_body)
        except _json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Request body is not valid JSON: {exc}") from exc

    if json_body is not None:
        try:
            json_body = enforce_policy_on_payload(json_body, user, settings)
        except Exception as exc:
            logger.exception("Policy enforcement failed for user_id=%s path=%s", user.user_id, path)
            raise HTTPException(status_code=500, detail="Governance policy enforcement falied.") from exc

    try:
        upstream_response = await proxy_client.forward(request, path, json_body)
    except UpstreamUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    response_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in {"content-encoding", "transfer-encoding", "content-length", "connection"}
    }
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": "request_failed", "detail": exc.detail})
