"""GET /v1/status - Beobachter-API fuer Konnektivitaets- und
Push-Health (AP-11 K8).

Im Unterschied zu ``/v1/health`` (reines Service-Lebenszeichen) liefert
dieser Endpoint:

- ``cp_gateway_connected``: Auth-Status des CP-Gateways. Mappt auf den
  ``AuthLifecycle.snapshot().auth_status``-Wert.
- ``last_frame_age_seconds``: Sekunden seit dem letzten WS-Frame
  (None, wenn noch nie ein Frame eingegangen ist - typisch beim
  Cold-Start, bevor der WSPushSource live ist).
- ``reconnect_attempt``: Aktueller Backoff-Counter des
  ``CPWebSocketClient`` (0 = idle).
- ``subscriptions_active``: Anzahl aktiver Eintraege in der
  ``SubscriptionRegistry``.

Scope ``read:status`` faellt mangels eigener Scope-Definition heute auf
``read:instruments`` (PSM-Pattern: was generelle Read-Operationen darf,
darf auch das Status-Polling). Eine eigene Scope-Definition kann eine
Folge-Karte aufnehmen.
"""
from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_INSTRUMENTS_READ, Token
from broker_gateway.cp.lifecycle import (
    AuthLifecycle,
    AuthStatus,
    get_cp_lifecycle,
)
from broker_gateway.streams.registry import SubscriptionRegistry


router = APIRouter(prefix="/status", tags=["status"])


class StatusResponse(BaseModel):
    cp_gateway_connected: bool = Field(
        description="True, wenn AuthLifecycle den Status OK meldet"
    )
    last_frame_age_seconds: float | None = Field(
        default=None,
        description="Sekunden seit dem letzten WS-Frame, None wenn ungesehen",
    )
    reconnect_attempt: int = Field(
        default=0, description="Aktueller WS-Backoff-Counter"
    )
    subscriptions_active: int = Field(
        default=0,
        description="Anzahl aktiver Eintraege in der SubscriptionRegistry",
    )


class StatusProbe:
    """Sammelt die vier Status-Felder aus drei Quellen.

    Wird im FastAPI-Lifespan instanziiert und ueber den Override-Pfad
    eingehaengt. Der WSPushSource pingt ``mark_frame()`` bei jedem
    erfolgreichen Frame an - das aktualisiert den Last-Frame-Marker.
    """

    def __init__(
        self,
        *,
        registry: SubscriptionRegistry | None = None,
        ws_reconnect_attempt: callable | None = None,  # type: ignore[assignment]
    ) -> None:
        self._registry = registry
        self._ws_reconnect_attempt = ws_reconnect_attempt
        self._last_frame_monotonic: float | None = None

    def mark_frame(self) -> None:
        self._last_frame_monotonic = time.monotonic()

    def snapshot(
        self, lifecycle: AuthLifecycle
    ) -> dict[str, object]:
        snapshot = lifecycle.snapshot()
        connected = snapshot.auth_status is AuthStatus.OK
        if self._last_frame_monotonic is None:
            age: float | None = None
        else:
            age = max(0.0, time.monotonic() - self._last_frame_monotonic)
        attempt = (
            self._ws_reconnect_attempt() if self._ws_reconnect_attempt else 0
        )
        active = self._registry.count() if self._registry is not None else 0
        return {
            "cp_gateway_connected": connected,
            "last_frame_age_seconds": age,
            "reconnect_attempt": int(attempt),
            "subscriptions_active": int(active),
        }


def get_status_probe() -> StatusProbe:
    raise RuntimeError(
        "get_status_probe muss per dependency_overrides gesetzt werden"
    )


@router.get(
    "",
    response_model=StatusResponse,
    summary="Konnektivitaets- und Push-Health (AP-11 K8)",
)
async def get_status(
    _scope: Annotated[
        Token, Depends(require_scope(SCOPE_INSTRUMENTS_READ))
    ],
    lifecycle: Annotated[AuthLifecycle, Depends(get_cp_lifecycle)],
    probe: Annotated[StatusProbe, Depends(get_status_probe)],
) -> StatusResponse:
    return StatusResponse.model_validate(probe.snapshot(lifecycle))


__all__ = [
    "StatusProbe",
    "StatusResponse",
    "get_status_probe",
    "router",
]
