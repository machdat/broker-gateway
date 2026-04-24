"""Async-Client für das interne IBKR Client Portal Gateway.

Methoden bilden die in der Mock-Fixture (tests/conftest.py) definierten
Endpunkte 1:1 ab. Pacing-Hook ist hier ein no-op - die echte Throttling
folgt in Karte 12 (Rate-Limit-Throttle).
"""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

import httpx


_DEFAULT_BASE_URL = "http://cpgateway:5000"
_DEFAULT_TIMEOUT_S = 10.0


PacingHook = Callable[[str, str], Awaitable[None]]
"""Async-Callable, das vor jedem Request mit (HTTP-Methode, URL-Pfad) gerufen wird.

Default-Implementierung ist ein no-op. Karte 12 wird hier ein echtes
Token-Bucket einhaengen.
"""


async def _noop_pacing(_method: str, _path: str) -> None:
    return None


class CPGatewayClient:
    """Schmale Hülle um httpx.AsyncClient mit ENV-konfigurierbarer Base-URL.

    Lebenszyklus:
    - `await client.aclose()` schließt den underlying httpx-Client.
    - Im FastAPI-Lifespan wird der Client einmal beim Startup angelegt und
      beim Shutdown geschlossen.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT_S,
        pacing_hook: PacingHook | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("BG_CP_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self._pacing = pacing_hook or _noop_pacing
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- Auth-Lifecycle-Endpunkte ----

    async def auth_status(self) -> dict[str, Any]:
        return await self._json("GET", "/iserver/auth/status")

    async def tickle(self) -> dict[str, Any]:
        return await self._json("POST", "/tickle")

    async def reauthenticate(self) -> dict[str, Any]:
        return await self._json("POST", "/reauthenticate")

    # ---- Generische Helfer (Folge-Karten benutzen sie für eigentliche Calls) ----

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        await self._pacing("GET", path)
        return await self._client.get(path, params=params)

    async def post(self, path: str, *, json: Any | None = None) -> httpx.Response:
        await self._pacing("POST", path)
        return await self._client.post(path, json=json)

    async def delete(self, path: str) -> httpx.Response:
        await self._pacing("DELETE", path)
        return await self._client.delete(path)

    async def _json(self, method: str, path: str) -> dict[str, Any]:
        await self._pacing(method, path)
        response = await self._client.request(method, path)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPError(f"Erwartete dict-Response von {method} {path}, bekam {type(data).__name__}")
        return data
