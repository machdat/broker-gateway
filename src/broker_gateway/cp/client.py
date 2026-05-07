"""Async-Client für das interne IBKR Client Portal Gateway.

Methoden bilden die in der Mock-Fixture (tests/conftest.py) definierten
Endpunkte 1:1 ab. Der `throttle`-Hook (Karte 12) zieht jeden Call durch
einen Token-Bucket pro Endpoint-Klasse und meldet 429-Responses
zurueck, damit der Bucket den Backoff anpasst.

Optionaler Recorder (Karte AP-02 #02): wenn die Umgebungsvariable
``BG_CP_RECORD_DIR`` gesetzt ist und kein expliziter ``http_client``
injiziert wurde, wird ein :class:`CPRecorder` automatisch an die
httpx-event-hooks angehaengt. Tests bleiben davon unbeeindruckt - sie
bringen ihren eigenen Client mit und setzen die ENV nicht.

Forensisches Wire-Log (AP-05 #03): zusaetzlich wird per Default ein
:class:`CPWireLogger` installiert, der jeden Roundtrip 1:1 als
``cp_wire``-Event nach ``cp_wire.log`` schreibt (gefiltert um Token-
Header). Abschaltbar ueber ``BG_CP_WIRE_LOG=off``.
"""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

import httpx

from broker_gateway.cp.recorder import CPRecorder
from broker_gateway.cp.wire_log import CPWireLogger
from broker_gateway.throttle.manager import ThrottleManager


_DEFAULT_BASE_URL = "http://cpgateway:5000/v1/api"
_DEFAULT_TIMEOUT_S = 10.0
_RECORD_DIR_ENV = "BG_CP_RECORD_DIR"
_WIRE_LOG_ENV = "BG_CP_WIRE_LOG"


PacingHook = Callable[[str, str], Awaitable[None]]
"""Async-Callable, das vor jedem Request mit (HTTP-Methode, URL-Pfad) gerufen wird.

Verbleibt als Test-Hook; der ThrottleManager (Karte 12) ist der
Produktions-Mechanismus und wird ueber den Parameter `throttle`
eingehaengt. Tests, die kein Throttling brauchen, lassen `throttle=None`
und das Pacing-Hook bleibt no-op.
"""


async def _noop_pacing(_method: str, _path: str) -> None:
    return None


def _wire_log_enabled() -> bool:
    raw = (os.environ.get(_WIRE_LOG_ENV) or "on").strip().lower()
    return raw not in ("off", "0", "false", "no")


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
        throttle: ThrottleManager | None = None,
        http_client: httpx.AsyncClient | None = None,
        cookies: httpx.Cookies | None = None,
        recorder: CPRecorder | None = None,
        wire_logger: CPWireLogger | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("BG_CP_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self._pacing = pacing_hook or _noop_pacing
        self._throttle = throttle
        self._owns_client = http_client is None
        if http_client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        else:
            self._client = http_client
        # httpx kopiert ein cookies=-Argument im Konstruktor in seinen
        # internen Jar — initiale Set-Cookies muessen also nachtraeglich
        # in self._client.cookies geseedet werden, damit shared State
        # erhalten bleibt. self.cookies (Property) zeigt unmittelbar
        # auf den Client-Jar.
        if cookies is not None:
            for cookie in cookies.jar:
                self._client.cookies.jar.set_cookie(cookie)

        # Recorder-Aktivierung: explizit injiziert > ENV > nichts.
        if recorder is None and self._owns_client:
            record_dir = os.environ.get(_RECORD_DIR_ENV)
            if record_dir:
                recorder = CPRecorder(record_dir)
        self._recorder = recorder
        if self._recorder is not None:
            self._recorder.install_into(self._client)

        # Wire-Logger-Aktivierung: explizit injiziert > ENV > Default 'on'.
        # Recorder und Wire-Logger koexistieren - Recorder normalisiert,
        # Wire-Logger schreibt 1:1; beide haengen sich an die httpx-Hooks.
        if wire_logger is None and _wire_log_enabled():
            wire_logger = CPWireLogger()
        self._wire_logger = wire_logger
        if self._wire_logger is not None:
            self._wire_logger.install_into(self._client)

        # Path-Override-Hook (Karte 406fce15 Phase B): patcht alle
        # cpgateway-Cookies im Jar nach jeder Response auf Path='/'.
        # Hintergrund: cpgateway setzt JSESSIONID + x-sess-uuid mit
        # Path=/sso. Service-Calls gehen aber an /v1/api/* — der
        # CookieJar wuerde die Session-Cookies ohne Override nicht
        # mitsenden, und alle authenticated Endpoints liefern 401.
        self._client.event_hooks.setdefault("response", []).append(
            self._force_root_path_on_session_cookies
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def cookies(self) -> httpx.Cookies:
        """Cookie-Jar des unterliegenden httpx.AsyncClient.

        Shared State: Cookies aus Set-Cookie-Response-Headern landen
        automatisch hier; Jar-Eintraege werden bei jedem Request
        mitgesendet (httpx-Default-Verhalten). Wird genutzt vom
        ``/v1/internal/seed-cookies``-Endpoint, um nach dem
        Browser-Login die cpgateway-Session-Cookies in den Service-
        Client zu importieren.
        """
        return self._client.cookies

    # ---- Auth-Lifecycle-Endpunkte ----

    async def auth_status(self) -> dict[str, Any]:
        return await self._json("GET", "/iserver/auth/status")

    async def tickle(self) -> dict[str, Any]:
        return await self._json("POST", "/tickle")

    async def reauthenticate(self) -> dict[str, Any]:
        return await self._json("POST", "/reauthenticate")

    async def sso_validate(self) -> dict[str, Any]:
        """Primaerer Keep-Alive-Endpunkt laut IBKR-OpenAPI-Spec
        (GET /sso/validate, Summary 'Validate SSO')."""
        return await self._json("GET", "/sso/validate")

    async def iserver_accounts(self) -> list[dict[str, Any]] | dict[str, Any]:
        """GET /iserver/accounts - Brokerage-Accounts. IBKR erwartet
        diesen Call beim Session-Start; ohne ihn bleiben einige
        nachfolgende Endpoints ungueltig."""
        await self._before("GET", "/iserver/accounts")
        response = await self._client.get("/iserver/accounts")
        self._after("GET", "/iserver/accounts", response)
        response.raise_for_status()
        return response.json()

    # ---- Generische Helfer (Folge-Karten benutzen sie für eigentliche Calls) ----

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        await self._before("GET", path)
        response = await self._client.get(path, params=params)
        self._after("GET", path, response)
        return response

    async def post(self, path: str, *, json: Any | None = None) -> httpx.Response:
        await self._before("POST", path)
        response = await self._client.post(path, json=json)
        self._after("POST", path, response)
        return response

    async def delete(self, path: str) -> httpx.Response:
        await self._before("DELETE", path)
        response = await self._client.delete(path)
        self._after("DELETE", path, response)
        return response

    async def _json(self, method: str, path: str) -> dict[str, Any]:
        await self._before(method, path)
        response = await self._client.request(method, path)
        self._after(method, path, response)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPError(f"Erwartete dict-Response von {method} {path}, bekam {type(data).__name__}")
        return data

    async def _before(self, method: str, path: str) -> None:
        await self._pacing(method, path)
        if self._throttle is not None:
            await self._throttle.acquire(method, path)

    def _after(self, method: str, path: str, response: httpx.Response) -> None:
        if self._throttle is None:
            return
        if response.status_code == 429:
            self._throttle.register_pacing_violation(method, path)
        else:
            self._throttle.register_success(method, path)

    async def _force_root_path_on_session_cookies(self, response: httpx.Response) -> None:
        """Schreibt Set-Cookie-Cookies aus der aktuellen Response auf Path='/' um.

        cpgateway setzt JSESSIONID + x-sess-uuid mit Path=/sso. Service-
        Calls gehen aber an /v1/api/* — der httpx-CookieJar wuerde diese
        Session-Cookies sonst nicht mitsenden. Wir patchen den Path nach
        jedem Response auf '/', damit alle Pfade matchen.

        Greift nur fuer Cookies, die in der aktuellen Response per
        Set-Cookie kamen (`response.cookies`). Cookies anderer Hosts, die
        bereits im Client-Jar liegen, bleiben unangetastet — der Hook
        agiert lokal auf dieser einen Roundtrip-Reaktion.
        """
        client_jar = self._client.cookies.jar
        for set_cookie in response.cookies.jar:
            if set_cookie.path == "/":
                continue
            # Aus dem Client-Jar entfernen (gleicher domain/path/name) und
            # mit Path='/' neu einsetzen. Direkte Mutation reicht nicht —
            # CookieJar nutzt path als Bucket-Key in seinem internen Dict.
            client_jar.clear(set_cookie.domain, set_cookie.path, set_cookie.name)
            set_cookie.path = "/"
            client_jar.set_cookie(set_cookie)
