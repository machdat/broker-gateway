"""Tests fuer CPWireLogger und seine Aktivierung im :class:`CPGatewayClient`.

Wir bauen jeden Test um drei Saeulen herum:

* :func:`broker_gateway.logging_setup.configure_logging` mit ``BG_LOG_DIR``
  in einem ``tmp_path`` - dadurch landen ``cp_wire``-Events tatsaechlich
  als JSON-Lines in ``cp_wire.log`` (kein Mock, sondern Production-Pfad).
* httpx.MockTransport gegen ein synthetisches Backend, damit der
  Wire-Logger einen echten ``httpx.Response`` zu Gesicht bekommt.
* :func:`structlog.contextvars.bind_contextvars` zum Setzen einer
  ``request_id``, die im Event auftauchen muss - das ist der
  Korrelations-Beweis zur Inbound-Middleware.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import structlog

from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.recorder import CPRecorder
from broker_gateway.cp.wire_log import CPWireLogger
from broker_gateway.logging_setup import configure_logging, reset_for_testing


@pytest.fixture(autouse=True)
def _reset_logging():
    reset_for_testing()
    yield
    reset_for_testing()
    structlog.contextvars.clear_contextvars()


def _read_cp_wire(log_dir: Path) -> list[dict]:
    log_path = log_dir / "cp_wire.log"
    if not log_path.exists():
        return []
    lines = [ln for ln in log_path.read_text("utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def _client_with_logger(
    handler, *, wire_logger: CPWireLogger | None = None
) -> tuple[httpx.AsyncClient, CPWireLogger]:
    """Baut einen MockTransport-Client und installiert einen Wire-Logger."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url="http://cpgateway:5000/v1/api",
        transport=transport,
    )
    logger = wire_logger or CPWireLogger()
    logger.install_into(client)
    return client, logger


@pytest.mark.asyncio
async def test_get_roundtrip_emits_one_cp_wire_event(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/iserver/auth/status")
        return httpx.Response(200, json={"authenticated": True})

    client, _ = _client_with_logger(handler)
    async with client:
        resp = await client.get(
            "/iserver/auth/status",
            params={"verbose": "1"},
        )
        assert resp.status_code == 200

    events = _read_cp_wire(tmp_path)
    assert len(events) == 1, events
    ev = events[0]
    assert ev["event"] == "cp_wire"
    assert ev["method"] == "GET"
    assert ev["path"] == "/iserver/auth/status"
    assert ev["query"] == {"verbose": "1"}
    assert ev["status"] == 200
    assert ev["response_body"] == {"authenticated": True}
    assert ev["request_body"] is None
    assert isinstance(ev["latency_ms"], (int, float))
    # Pflichtfelder fuer Header existieren als Dict (auch wenn leer).
    assert isinstance(ev["request_headers"], dict)
    assert isinstance(ev["response_headers"], dict)


@pytest.mark.asyncio
async def test_request_id_from_contextvars_appears_in_event(
    tmp_path: Path, monkeypatch
) -> None:
    """Korrelation inbound.log <-> cp_wire.log: request_id propagiert via structlog."""
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client, _ = _client_with_logger(handler)
    structlog.contextvars.bind_contextvars(request_id="req-1234abcd")
    try:
        async with client:
            await client.get("/tickle")
    finally:
        structlog.contextvars.clear_contextvars()

    events = _read_cp_wire(tmp_path)
    assert len(events) == 1, events
    assert events[0]["request_id"] == "req-1234abcd"


@pytest.mark.asyncio
async def test_authorization_and_secrets_never_appear(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"Set-Cookie": "session=hidden-cookie; Path=/"},
        )

    client, _ = _client_with_logger(handler)
    async with client:
        await client.get(
            "/iserver/auth/status",
            headers={
                "Authorization": "Bearer secret-token-xyz",
                "Cookie": "csrftoken=cookie-value",
                "X-API-Key": "leak-me-not",
                "X-Auth-Token": "another-secret",
                "Proxy-Authorization": "Basic proxy-secret",
            },
        )

    events = _read_cp_wire(tmp_path)
    assert len(events) == 1
    ev = events[0]
    req_keys = {k.lower() for k in ev["request_headers"]}
    resp_keys = {k.lower() for k in ev["response_headers"]}
    for forbidden in (
        "authorization",
        "cookie",
        "x-api-key",
        "x-auth-token",
        "proxy-authorization",
    ):
        assert forbidden not in req_keys, ev["request_headers"]
    assert "set-cookie" not in resp_keys, ev["response_headers"]

    # Doppelter Boden: keiner der Werte taucht irgendwo in der Log-Datei auf.
    raw = (tmp_path / "cp_wire.log").read_text("utf-8")
    for forbidden_value in (
        "secret-token-xyz",
        "cookie-value",
        "leak-me-not",
        "another-secret",
        "proxy-secret",
        "hidden-cookie",
    ):
        assert forbidden_value not in raw, raw


@pytest.mark.asyncio
async def test_post_body_is_logged_unchanged_no_normalization(
    tmp_path: Path, monkeypatch
) -> None:
    """Forensische Treue: Order-IDs etc. werden NICHT normalisiert."""
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()

    sent_payload = {
        "orders": [
            {
                "conid": 265598,
                "side": "BUY",
                "quantity": 10,
                "order_id": "abcd-1234-DEADBEEF",
                "session": "sess-uuid-xxxx",
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ack": True, "order_id": "abcd-1234-DEADBEEF"},
        )

    client, _ = _client_with_logger(handler)
    async with client:
        await client.post(
            "/iserver/account/U25235077/orders",
            json=sent_payload,
        )

    events = _read_cp_wire(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["method"] == "POST"
    # 1:1 zum gesendeten Payload, keine Platzhalter wie <ORDER_ID_001>
    assert ev["request_body"] == sent_payload
    assert ev["response_body"] == {
        "ack": True,
        "order_id": "abcd-1234-DEADBEEF",
    }
    raw = (tmp_path / "cp_wire.log").read_text("utf-8")
    assert "<ORDER_ID" not in raw
    assert "<SESSION_ID" not in raw


@pytest.mark.asyncio
async def test_4xx_and_5xx_responses_are_logged(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/forbidden"):
            return httpx.Response(401, json={"error": "auth_required"})
        return httpx.Response(500, json={"error": "internal"})

    client, _ = _client_with_logger(handler)
    async with client:
        await client.get("/forbidden")
        await client.get("/broken")

    events = _read_cp_wire(tmp_path)
    statuses = sorted(e["status"] for e in events)
    assert statuses == [401, 500], events


@pytest.mark.asyncio
async def test_wire_log_off_emits_no_events(
    tmp_path: Path, monkeypatch
) -> None:
    """Mit BG_CP_WIRE_LOG=off installiert der Default-Konstruktor keinen Logger."""
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("BG_CP_WIRE_LOG", "off")
    configure_logging()

    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": True})
    )
    injected = httpx.AsyncClient(
        base_url="http://cpgateway:5000/v1/api", transport=transport
    )
    client = CPGatewayClient(http_client=injected)
    try:
        assert client._wire_logger is None
        await client.get("/iserver/auth/status")
    finally:
        await client.aclose()

    assert _read_cp_wire(tmp_path) == []


@pytest.mark.asyncio
async def test_wire_log_default_on_via_env(
    tmp_path: Path, monkeypatch
) -> None:
    """Default ist 'on'; ENV nicht gesetzt -> Logger wird installiert."""
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("BG_CP_WIRE_LOG", raising=False)
    configure_logging()

    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": True})
    )
    injected = httpx.AsyncClient(
        base_url="http://cpgateway:5000/v1/api", transport=transport
    )
    client = CPGatewayClient(http_client=injected)
    try:
        assert isinstance(client._wire_logger, CPWireLogger)
        await client.get("/tickle")
    finally:
        await client.aclose()

    events = _read_cp_wire(tmp_path)
    assert len(events) == 1
    assert events[0]["path"].endswith("/tickle")


@pytest.mark.asyncio
async def test_logger_failure_does_not_break_request(
    tmp_path: Path, monkeypatch
) -> None:
    """Ein Schreib-Fehler in self._log.info darf den httpx-Call nicht killen.

    Wir patchen den structlog-Bound-Logger so, dass ``info`` raised - das
    triggert das try/except in :meth:`CPWireLogger._on_response`. Wenn die
    Exception bis zu httpx durchschlaegt, schlaegt der GET fehl.
    """
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()

    class _BrokenInfo:
        def info(self, *args, **kwargs) -> None:
            raise RuntimeError("simuliertes Logging-Versagen")

    wire_logger = CPWireLogger()
    wire_logger._log = _BrokenInfo()  # type: ignore[assignment]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client, _ = _client_with_logger(handler, wire_logger=wire_logger)
    async with client:
        try:
            resp = await client.get("/iserver/auth/status")
        except RuntimeError:
            pytest.fail(
                "Logger-Exception ist bis zum httpx-Aufrufer durchgeschlagen - "
                "_on_response muss intern try/except'en."
            )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_recorder_and_wire_logger_coexist(
    tmp_path: Path, monkeypatch
) -> None:
    """Beide Hooks koexistieren: Recorder schreibt JSON-File, Wire-Logger schreibt Log."""
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BG_CP_RECORD_DIR", str(tmp_path / "rec"))
    monkeypatch.delenv("BG_CP_WIRE_LOG", raising=False)
    configure_logging()

    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": True})
    )
    injected = httpx.AsyncClient(
        base_url="http://cpgateway:5000/v1/api", transport=transport
    )
    # Recorder explizit injizieren - die ENV-Auto-Aktivierung greift im
    # CPGatewayClient nur fuer Default-erzeugte Clients (_owns_client),
    # nicht fuer per http_client injizierte. Wire-Logger-Default-on
    # zeigt seine Wirkung trotzdem.
    recorder = CPRecorder(tmp_path / "rec")
    client = CPGatewayClient(http_client=injected, recorder=recorder)
    try:
        await client.get("/iserver/auth/status")
    finally:
        await client.aclose()

    recorder_files = list((tmp_path / "rec").glob("*.json"))
    assert len(recorder_files) == 1, recorder_files
    events = _read_cp_wire(tmp_path / "logs")
    assert len(events) == 1, events
