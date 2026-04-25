"""Tests fuer CPRecorder + normalize_response."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.normalize import normalize_response
from broker_gateway.cp.recorder import CPRecorder


def _client_with(recorder: CPRecorder, handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url="http://cpgateway:5000/v1/api",
        transport=transport,
    )
    recorder.install_into(client)
    return client


@pytest.mark.asyncio
async def test_records_three_distinct_calls_to_three_files(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"path": request.url.path})

    recorder = CPRecorder(tmp_path)
    async with _client_with(recorder, handler) as client:
        await client.get("/iserver/auth/status")
        await client.get("/iserver/account/U25235077/positions")
        await client.post("/tickle", json={})

    files = sorted(p.name for p in tmp_path.glob("*.json"))
    assert len(files) == 3, f"expected 3 files, got {files}"
    for fp in tmp_path.glob("*.json"):
        env = json.loads(fp.read_text("utf-8"))
        assert env["normalized"] is True
        assert env["response"]["status_code"] == 200
        assert "request" in env and "response" in env


@pytest.mark.asyncio
async def test_first_call_prime_numbering(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"called": True})

    recorder = CPRecorder(tmp_path)
    async with _client_with(recorder, handler) as client:
        await client.get(
            "/iserver/marketdata/snapshot",
            params={"conids": "265598", "fields": "31"},
        )
        await client.get(
            "/iserver/marketdata/snapshot",
            params={"conids": "265598", "fields": "31"},
        )

    files = sorted(p.name for p in tmp_path.glob("*.json"))
    assert len(files) == 2, files
    assert any(name.endswith("_01.json") for name in files), files
    assert any(name.endswith("_02.json") for name in files), files


@pytest.mark.asyncio
async def test_secrets_in_headers_are_never_persisted(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"Set-Cookie": "session=abc123; Path=/"},
        )

    recorder = CPRecorder(tmp_path)
    async with _client_with(recorder, handler) as client:
        await client.get(
            "/iserver/auth/status",
            headers={
                "Authorization": "Bearer secret-token-xyz",
                "Cookie": "csrftoken=cookie-value",
                "X-API-Key": "leak-me-not",
            },
        )

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1

    env = json.loads(files[0].read_text("utf-8"))
    req_header_keys = {k.lower() for k in env["request"]["headers"]}
    resp_header_keys = {k.lower() for k in env["response"]["headers"]}
    assert "authorization" not in req_header_keys
    assert "cookie" not in req_header_keys
    assert "x-api-key" not in req_header_keys
    assert "set-cookie" not in resp_header_keys

    # Doppelter Boden: keiner der Werte taucht irgendwo in der Datei auf.
    raw = files[0].read_text("utf-8")
    assert "secret-token-xyz" not in raw
    assert "leak-me-not" not in raw
    assert "abc123" not in raw
    assert "cookie-value" not in raw


def test_normalize_response_replaces_timestamps_and_ids() -> None:
    payload = {
        "order_id": "abcd-1234",
        "execution_id": "exec-9999",
        "session": "sess-uuid-xxxx",
        "trade_time": "2026-04-25 10:00:00",
        "recorded_at": "2026-04-25T10:00:00Z",
        "price": "150.00",
        "avgCost": "145.00",
        "conid": 265598,
        "filled_quantity": "10",
        "nested": [
            {"order_id": "abcd-1234", "iso": "2026-04-25T10:00:00.123Z"},
            {"order_id": "another-id", "executionId": "exec-9999"},
        ],
    }
    result = normalize_response(payload, "/iserver/account/orders")

    assert result["order_id"] == "<ORDER_ID_001>"
    assert result["execution_id"] == "<EXEC_ID_001>"
    assert result["session"] == "<SESSION_ID>"
    assert result["trade_time"] == "<TIMESTAMP>"
    assert result["recorded_at"] == "<TIMESTAMP>"
    # Preise + Marktdaten bleiben unangetastet (Default).
    assert result["price"] == "150.00"
    assert result["avgCost"] == "145.00"
    assert result["conid"] == 265598
    assert result["filled_quantity"] == "10"
    # Counter behaelt Referenzen: gleicher Roh-Wert -> gleicher Platzhalter.
    assert result["nested"][0]["order_id"] == "<ORDER_ID_001>"
    assert result["nested"][0]["iso"] == "<TIMESTAMP>"
    assert result["nested"][1]["order_id"] == "<ORDER_ID_002>"
    assert result["nested"][1]["executionId"] == "<EXEC_ID_001>"


def test_normalize_response_with_normalize_prices_true_replaces_prices() -> None:
    payload = {
        "price": "150.00",
        "mktValue": 1505.0,
        "31": "150.50",
        "84": "150.45",
    }
    result = normalize_response(
        payload, "/iserver/marketdata/snapshot", normalize_prices=True
    )
    assert result["price"] == "<PRICE>"
    assert result["mktValue"] == "<PRICE>"
    assert result["31"] == "<PRICE>"
    assert result["84"] == "<PRICE>"


@pytest.mark.asyncio
async def test_cpgatewayclient_recorder_inactive_without_env(monkeypatch) -> None:
    monkeypatch.delenv("BG_CP_RECORD_DIR", raising=False)
    client = CPGatewayClient()
    try:
        assert client._recorder is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cpgatewayclient_recorder_active_with_env(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BG_CP_RECORD_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    injected = httpx.AsyncClient(
        base_url="http://cpgateway:5000/v1/api", transport=transport
    )
    # ENV-Aktivierung greift nur fuer den default-erzeugten Client. Wir
    # ueberschreiben sie hier explizit, indem wir den Recorder direkt
    # mitgeben - so kann der Test gegen MockTransport sprechen.
    recorder = CPRecorder(tmp_path)
    client = CPGatewayClient(http_client=injected, recorder=recorder)
    try:
        assert client._recorder is recorder
        await client.get("/iserver/auth/status")
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1, f"recorder did not write a file: {files}"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cpgatewayclient_recorder_built_from_env_when_no_client_injected(
    monkeypatch, tmp_path: Path
) -> None:
    """ENV-Aktivierung im Default-Konstruktor: Recorder wird automatisch erzeugt."""
    monkeypatch.setenv("BG_CP_RECORD_DIR", str(tmp_path))
    client = CPGatewayClient()
    try:
        assert client._recorder is not None
        assert client._recorder.record_dir == tmp_path
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_request_body_is_recorded(tmp_path: Path) -> None:
    """Recorder persistiert den POST-Body (z.B. Order-Spec) als body_json."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ack": True})

    recorder = CPRecorder(tmp_path)
    async with _client_with(recorder, handler) as client:
        await client.post(
            "/iserver/account/U25235077/orders",
            json={"orders": [{"conid": 265598, "side": "BUY", "quantity": 1}]},
        )

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    env = json.loads(files[0].read_text("utf-8"))
    assert env["request"]["body_json"] == {
        "orders": [{"conid": 265598, "side": "BUY", "quantity": 1}]
    }
