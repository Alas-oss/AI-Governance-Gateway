from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import Request

from app.config import Settings

logger = logging.getLogger(__name__)

class UpstreamUnavailableError(Exception):
    pass

class UpstreamProxyClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        timeout = httpx.Timeout(
            timeout=self._settings.upstream_timeout_seconds,
            connect=self._settings.upstream_connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            base_url=self._settings.upstream_base_url,
            timeout=timeout,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        logger.info("Upstream proxy client start (base_url=%s)", self._settings.upstream_base_url)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("Upstream proxy client closed.")

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("UpstreamProxyClient.forward() called before start(). Check app lifespan wiring.")
        return self._client

    def _build_forward_headers(self, request: Request) -> Dict[str, str]:
        hop_by_hop = {h.lower() for h in self._settings.hop_by_hop_headers}
        return {k: v for k, v in request.headers.items() if k.lower() not in hop_by_hop}

    async def forward(self, request: Request, path: str, json_body: Optional[Dict[str, Any]]) -> httpx.Response:
        headers = self._build_forward_headers(request)
        upstream_path = f"/{path.lstrip('/')}"

        try:
            upstream_response = await self.client.request(
                method=request.method,
                url=upstream_path,
                params=dict(request.query_params),
                headers=headers,
                json=json_body,
            )
        except httpx.ConnectTimeout as exc:
            logger.error("Connext timeout reaching upstream at %s", upstream_path)
            raise UpstreamUnavailableError("Upstream AI agent connection timed out.") from exc
        except httpx.ReadTimeout as exc:
            logger.error("Read timeout waiting on upstream %s", upstream_path)
            raise UpstreamUnavailableError("Upstream AI agent response timed out.") from exc
        except httpx.HTTPError as exc:
            logger.error("Transport error reaching upstream at %s: %s", upstream_path, exc)
            raise UpstreamUnavailableError("Upstream AI agent is unreachable.") from exc


        return upstream_response