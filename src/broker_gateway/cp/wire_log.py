"""Forensisches Wire-Log fuer den IBKR-CP-Gateway-Verkehr.

Schreibt **jeden** httpx-Roundtrip 1:1 als structlog-Event ``cp_wire`` an
den Logger ``broker_gateway.cp.wire`` (Logging-Backbone routet diesen
Strang nach ``cp_wire.log``). Im Gegensatz zum :class:`CPRecorder`
werden Order-IDs, Timestamps und Session-IDs **nicht** normalisiert -
forensische Treue hat hier Vorrang vor Determinismus, weil genau die
normalisierten Felder oft die Ursache eines Live-Fehlers sind.

Aktivierung per ENV ``BG_CP_WIRE_LOG`` (Default ``on``); ``off`` schaltet
das Mitschreiben komplett ab. Token-Werte (Authorization, Cookie, ...)
werden ueber :mod:`broker_gateway.cp.redaction` gefiltert und tauchen
nie im Log auf - das ist die einzige Aenderung gegenueber "rohem
Roundtrip". structlog-ContextVars (``request_id`` aus der
Inbound-Middleware) werden vom JSONRenderer automatisch in jedes Event
gemerged - dadurch ist der Join inbound.log <-> cp_wire.log ohne
Zusatz-Code moeglich.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import httpx

from broker_gateway.cp.redaction import filter_headers
from broker_gateway.logging_setup import get_logger


_FALLBACK_LOG = logging.getLogger(__name__)
_LATENCY_KEY = "broker_gateway.cp.wire_log.start_monotonic"


class CPWireLogger:
    """Schreibt pro CP-Roundtrip ein ``cp_wire``-Event.

    Lebenszyklus:

    * :meth:`install_into` haengt einen request- und einen response-
      event_hook an einen :class:`httpx.AsyncClient`.
    * Der Logger ist immer aktiv, sobald installiert. Die Aktivierungs-
      logik (ENV ``BG_CP_WIRE_LOG``) liegt im :class:`CPGatewayClient`,
      damit der Wire-Logger selbst frei von Konfigurations-Lookups
      bleibt und in Tests deterministisch instanziierbar ist.
    """

    def __init__(self, *, base_path: str | None = None) -> None:
        # Pfad-Prefix (z.B. "/v1/api"), der beim Loggen abgeschnitten
        # werden kann. Default None = nicht strippen, sondern den vollen
        # request.url.path schreiben.
        self.base_path: str | None = (
            base_path.rstrip("/") if base_path else None
        )
        self._log = get_logger("broker_gateway.cp.wire")

    def install_into(self, client: httpx.AsyncClient) -> None:
        if self.base_path is None:
            client_path = (client.base_url.path or "").rstrip("/")
            self.base_path = client_path
        request_hooks = list(client.event_hooks.get("request", []))
        response_hooks = list(client.event_hooks.get("response", []))
        request_hooks.append(self._on_request)
        response_hooks.append(self._on_response)
        client.event_hooks = {
            "request": request_hooks,
            "response": response_hooks,
        }

    async def _on_request(self, request: httpx.Request) -> None:
        # Start-Timestamp am Request hinterlegen; response-Hook liest
        # ihn aus, um latency_ms zu berechnen. ``request.extensions`` ist
        # ein dict, das httpx fuer transport-spezifische Metadaten
        # benutzt - wir benutzen einen eindeutigen Schluessel.
        request.extensions[_LATENCY_KEY] = time.monotonic()

    async def _on_response(self, response: httpx.Response) -> None:
        try:
            await response.aread()
        except Exception:  # noqa: BLE001
            # Selbst wenn das Body-Lesen scheitert, soll der httpx-Call
            # weiterlaufen. Es bleibt nur ein Wire-Event ohne Body.
            response_body = None
            response_body_b64 = None
        else:
            response_body, response_body_b64 = _decode_body(
                response.content, response.headers.get("content-type")
            )

        request = response.request
        request_body, request_body_b64 = _decode_body(
            request.content, request.headers.get("content-type")
        )

        path = self._strip_base_path(request.url.path)
        query = _query_dict(request.url)

        latency_ms: float | None = None
        start = request.extensions.get(_LATENCY_KEY)
        if isinstance(start, (int, float)):
            latency_ms = round((time.monotonic() - float(start)) * 1000.0, 2)

        event_kwargs: dict[str, Any] = {
            "method": request.method.upper(),
            "path": path,
            "query": query,
            "request_headers": filter_headers(request.headers),
            "request_body": request_body,
            "status": response.status_code,
            "response_headers": filter_headers(response.headers),
            "response_body": response_body,
            "latency_ms": latency_ms,
        }
        if request_body_b64 is not None:
            event_kwargs["request_body_b64"] = request_body_b64
        if response_body_b64 is not None:
            event_kwargs["response_body_b64"] = response_body_b64

        try:
            self._log.info("cp_wire", **event_kwargs)
        except Exception as exc:  # noqa: BLE001
            # Logging-Fehler darf den Live-Call nie brechen - analog
            # zum Recorder-Verhalten. Fallback ueber stdlib-Logger als
            # Best-Effort, damit der Vorfall sichtbar bleibt.
            _FALLBACK_LOG.warning(
                "CPWireLogger konnte %s %s nicht persistieren: %s",
                request.method, request.url.path, exc,
            )

    def _strip_base_path(self, path: str) -> str:
        if not self.base_path:
            return path
        if path.startswith(self.base_path):
            stripped = path[len(self.base_path):]
            return stripped if stripped.startswith("/") else "/" + stripped
        return path


def _query_dict(url: httpx.URL) -> dict[str, str]:
    return {key: url.params[key] for key in sorted(url.params.keys())}


def _decode_body(body: bytes, content_type: str | None) -> tuple[Any, str | None]:
    """Liefert ``(value, b64)`` analog zur Inbound-Body-Logik.

    * ``value`` ist parsed JSON (dict/list/scalar), UTF-8-String oder ``None``.
    * ``b64`` ist nur gesetzt, wenn der Body nicht UTF-8-dekodierbar ist
      (Binaerdaten); dann ist ``value=None`` und der Base64-Fallback
      dient als verlustfreier Forensik-Anker.

    Wichtig: hier wird **nicht** ueber
    :func:`broker_gateway.cp.normalize.normalize_response` normalisiert.
    """
    if not body:
        return None, None
    ct_lower = (content_type or "").lower()
    looks_like_json = "json" in ct_lower or body[:1] in (b"{", b"[")
    if looks_like_json:
        try:
            return json.loads(body.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    try:
        return body.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, base64.b64encode(body).decode("ascii")


__all__ = ["CPWireLogger"]
