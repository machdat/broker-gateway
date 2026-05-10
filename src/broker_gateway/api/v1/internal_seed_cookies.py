"""POST /v1/internal/seed-cookies - Browser-Cookies in den Service-Jar uebernehmen.

Karte 406fce15 Phase C. Nach dem Pi-Browser-Login (siehe
``ops/cp-login-pi-nsenter.sh``) liegen JSESSIONID + x-sess-uuid im
Browser-Profil; der Service hat aber einen separaten httpx-Cookie-Jar.
Dieser Endpoint nimmt die beiden Cookies ueber einen Operator-Aufruf
(z.B. curl mit JSON-Body, ``X-API-Key``-Token mit ``admin:*``) entgegen,
seedet sie in den Lifecycle-Client und triggert sofort einen Tick,
damit der naechste internal/health-Snapshot den frischen Auth-Status
zeigt.

Cookies werden mit Path="/" gesetzt (httpx-Default), damit sie sowohl
fuer ``/v1/api/*`` als auch fuer ``/sso/*`` matchen — der Phase-B-Hook
sorgt zusaetzlich dafuer, dass spaetere Server-Set-Cookies (Path=/sso)
ebenfalls auf "/" umgeschrieben werden.

Falls der Lifecycle-Client und der Services-Client unterschiedliche
Instanzen sind (Test-Setup mit injiziertem Lifecycle), wird der Seed
in beide Jars geschrieben — sonst sind Service-Calls (Quotes/Orders)
trotz gueltiger Lifecycle-Session weiter 401.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle, AuthStatus, get_cp_lifecycle


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/internal", tags=["internal-seed-cookies"])


_SEEDED_NAMES = ("JSESSIONID", "x-sess-uuid")


class SeedCookiesRequest(BaseModel):
    jsessionid: str = Field(
        min_length=1,
        description="Wert des JSESSIONID-Cookies aus dem Pi-Browser-Profil "
        "(DevTools -> Application -> Cookies fuer http://<cp-ip>:5000).",
    )
    x_sess_uuid: str = Field(
        min_length=1,
        description="Wert des x-sess-uuid-Cookies — kommt zusammen mit "
        "JSESSIONID nach dem Login-Submit.",
    )
    host: str | None = Field(
        default=None,
        description="Optional rein informativ: aus welchem Browser-Origin "
        "die Cookies kamen (z.B. '172.23.0.2:5000'). Wird nicht in den "
        "Cookie-Jar uebernommen — der Service-Client kennt seinen "
        "eigenen Host und httpx setzt die Domain beim Request automatisch.",
    )
    ssodh_init: bool = Field(
        default=True,
        description="Phase D (default on): nach dem Seed POST "
        "/iserver/auth/ssodh/init mit body {keepAlive: true} aufrufen, "
        "damit cpgateway die Session offiziell etabliert und ggf. "
        "Wildcard-Cookies setzt. Bei Fehler bleibt der Phase-B-Path-"
        "Override als Fallback aktiv (Endpoint antwortet weiter 200).",
    )


class SeedCookiesResponse(BaseModel):
    seeded: list[str] = Field(
        description="Cookie-Namen, die in den Service-Jar geschrieben wurden."
    )
    seeded_in_services_client: bool = Field(
        description="True, wenn zusaetzlich der Services-Client einen "
        "eigenen Jar hatte (Test-Setup) und ebenfalls befuellt wurde."
    )
    auth_status: AuthStatus = Field(
        description="auth_status nach dem sofortigen Tick — auf 'ok' "
        "geht er typischerweise erst nach 1-2 weiteren Ticks im "
        "Hintergrund-Loop."
    )
    tick_triggered: bool = Field(
        description="True, wenn der sofortige Tick durchlief (auch bei "
        "Status != ok). False nur, wenn der Tick eine Exception warf — "
        "dann steckt der Fehler im Server-Log."
    )
    ssodh_init_status: str = Field(
        description="Status des optionalen ssodh-Init-Calls: 'ok' bei "
        "erfolgreicher 2xx-Antwort, 'error' bei HTTP-Fehler / Exception, "
        "'skipped' wenn ssodh_init=false im Request war.",
    )


def _seed_into(client: CPGatewayClient, jsessionid: str, x_sess_uuid: str) -> None:
    """Setzt JSESSIONID + x-sess-uuid mit Path='/' in den Client-Jar.

    Domain bleibt leer — httpx setzt sie beim naechsten Request auf den
    Request-Host. Das ist die einzige Variante, bei der der
    DefaultCookiePolicy von ``http.cookiejar`` die Cookies auch
    tatsaechlich sendet (siehe Phase A Test-Erkenntnisse: explizite
    Domain-Strings auf TLD-losen Hostnames blocken den Send).
    """
    client.cookies.set("JSESSIONID", jsessionid)
    client.cookies.set("x-sess-uuid", x_sess_uuid)


@router.post(
    "/seed-cookies",
    response_model=SeedCookiesResponse,
    summary="Browser-Cookies in den Service-Jar seeden (admin:*)",
)
async def seed_cookies(
    request: Request,
    body: SeedCookiesRequest,
    _admin: Annotated[Token, Depends(require_scope(SCOPE_ADMIN_ALL))],
    lifecycle: Annotated[AuthLifecycle, Depends(get_cp_lifecycle)],
) -> SeedCookiesResponse:
    """Seed-Cookies einsetzen + sofortigen Tick triggern.

    Klartext-Cookies werden nicht geloggt — nur die Cookie-Namen und
    der resultierende Auth-Status. Der Wire-Logger redaktiert den
    folgenden Tickle-Roundtrip ohnehin (Header-Filter).

    AP ``2a203c58-...`` Phase 6: Cookie-Seeding ist semantisch nur
    fuer cpgateway sinnvoll. Im tws-Mode existiert kein cp-Client und
    keine Browser-Login-Recovery — der Endpoint antwortet mit 503 +
    ``not_applicable_in_tws_mode``, statt blind auf einen nicht-
    vorhandenen Cookie-Jar zu schreiben.
    """
    if getattr(request.app.state, "backend", "cp") == "tws":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "not_applicable_in_tws_mode",
                "message": (
                    "Cookie-Seeding ist nur unter Profile cp-legacy "
                    "verfuegbar. Im tws-Mode gibt es keinen cpgateway-"
                    "Browser-Login zu seeden."
                ),
            },
        )

    _seed_into(lifecycle.client, body.jsessionid, body.x_sess_uuid)

    services_client: CPGatewayClient | None = getattr(
        request.app.state, "_services_client", None
    )
    seeded_in_services = False
    if services_client is not None and services_client is not lifecycle.client:
        _seed_into(services_client, body.jsessionid, body.x_sess_uuid)
        seeded_in_services = True

    logger.info(
        "seed_cookies",
        extra={
            "event": "seed_cookies",
            "seeded": list(_SEEDED_NAMES),
            "host": body.host,
            "services_client_seeded": seeded_in_services,
            "ssodh_init": body.ssodh_init,
        },
    )

    ssodh_status = await _maybe_run_ssodh_init(lifecycle.client, body.ssodh_init)

    tick_ok = True
    try:
        await lifecycle.tick_once()
    except Exception:
        # tick_once kapselt selbst die meisten httpx-Fehler; Exceptions
        # hier sind eher Programmierfehler — wir wollen den Endpoint
        # trotzdem mit dem aktuellen Snapshot antworten lassen.
        logger.exception("seed_cookies.tick_once_failed")
        tick_ok = False

    snap = lifecycle.snapshot()
    return SeedCookiesResponse(
        seeded=list(_SEEDED_NAMES),
        seeded_in_services_client=seeded_in_services,
        auth_status=snap.auth_status,
        tick_triggered=tick_ok,
        ssodh_init_status=ssodh_status,
    )


async def _maybe_run_ssodh_init(client: CPGatewayClient, enabled: bool) -> str:
    """Phase D: optional /iserver/auth/ssodh/init mit keepAlive=true.

    Bei Erfolg setzt cpgateway typischerweise frische Cookies (ggf. mit
    Wildcard-Path) und etabliert die iserver-Bridge. Bei Fehler bleibt
    der Phase-B-Path-Override als Fallback — der Endpoint antwortet
    trotzdem 200, der Operator sieht ``ssodh_init_status="error"`` und
    kann im Server-Log nachsehen.
    """
    if not enabled:
        return "skipped"
    try:
        response = await client.post(
            "/iserver/auth/ssodh/init", json={"keepAlive": True}
        )
    except Exception:
        logger.exception("seed_cookies.ssodh_init_exception")
        return "error"
    if response.status_code >= 400:
        logger.warning(
            "seed_cookies.ssodh_init_http_error",
            extra={
                "event": "seed_cookies.ssodh_init",
                "status_code": response.status_code,
            },
        )
        return "error"
    return "ok"
