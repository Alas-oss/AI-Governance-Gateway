from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from app.auth.jwt_utils import TokenValidationError, authenticate_bearer_token
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

OPEN_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class JWTAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, settings: Settings | None = None) -> None:
        super().__init__(app)
        self._settings = settings or get_settings()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in OPEN_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("authorization")
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "detail": "Missing or malformed Authorization header. Expected 'Bearer <token>'.",
                },
            )

        token = auth_header.split(" ", 1)[1].strip()

        try:
            user_context = authenticate_bearer_token(token, self._settings)
        except TokenValidationError as exc:
            logger.info("Rejected request to %s: %s", request.url.path, exc.message)
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": "unauthorized", "detail": exc.message},
            )
        except Exception:  # noqa: BLE001 defensive: never leak internals for auth failures
            logger.exception("Unexpected error during authentication.")
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error", "detail": "Authentication subsystem failure."},
            )

        request.state.user = user_context
        
        return await call_next(request)