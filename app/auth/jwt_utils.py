from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import jwt
from jwt import PyJWTError

from app.config import Settings

logger = logging.getLogger(__name__)

class ClearanceLevel(str, Enum):
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    ADMIN = "admin"

class TokenValidationError(Exception):
    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

@dataclass(frozen=True)
class UserContext:
    user_id: str
    department: str
    clearance_level: ClearanceLevel
    email: Optional[str] = None
    raw_claims: Optional[Dict[str, Any]] = None

REQUIRED_CLAIMS = ("sub", "department", "clearance_level")

def decode_and_verify_token(token: str, settings: Settings) -> Dict[str, Any]:
    if not token:
        raise TokenValidationError("Missing bearer token.")

    if not settings.jwt_public_key:
        raise TokenValidationError(
            "Gateway is misconfigured: no JWT verification key is loaded.", status_code=500
        )

    decode_kwargs: Dict[str, Any] = {
        "algorithms": [settings.jwt_algorithm],
        "options": {"require": ["exp", "iat", "sub"]},
        "leeway": settings.jwt_leeway_seconds,
    }
    if settings.jwt_audience:
        decode_kwargs["audience"] = settings.jwt_audience
    if settings.jwt_issuer:
        decode_kwargs["issuer"] = settings.jwt_issuer

    try:
        claims: Dict[str, Any] = jwt.decode(token, settings.jwt_public_key, **decode_kwargs)
    except jwt.ExpiredSignatureError as exc:
        raise TokenValidationError("Bearer token has expired.") from exc
    except PyJWTError as exc:
        logger.warning("JWT verification failed: %s", exc)
        raise TokenValidationError("Bearer token failed signature/claim verification.") from exc

    return claims

def build_user_context(claims: Dict[str, Any]) -> UserContext:
    missing = [c for c in REQUIRED_CLAIMS if not claims.get(c)]
    if missing:
        raise TokenValidationError(f"Token is missing required claim(s): {', '.join(missing)}.")

    raw_level = str(claims["clearance_level"]).strip().lower()
    try:
        clearance_level = ClearanceLevel(raw_level)
    except ValueError as exc:
        raise TokenValidationError(f"Unknown clearance_level '{raw_level}' in token.") from exc

    return UserContext(
        user_id=str(claims["sub"]),
        department=str(claims["department"]),
        clearance_level=clearance_level,
        email=claims.get("email"),
        raw_claims=claims,
    )

def authenticate_bearer_token(token: str, settings: Settings) -> UserContext:
    claims = decode_and_verify_token(token, settings)
    return build_user_context(claims)
