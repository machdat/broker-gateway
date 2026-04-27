"""Tests fuer scripts/recording_session.py.

Faehrt das happy-path-Skript gegen httpx.MockTransport, sodass die
gesamte Endpunkt-Sequenz und die Recording-Mechanik getestet werden,
ohne dass ein echtes CP-Gateway laufen muss.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

# scripts/ ist nicht als Package importierbar - wir laden das Modul von Hand.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "recording_session.py"


def _load_recording_session() -> ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("recording_session", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["recording_session"] = module
    spec.loader.exec_module(module)
    return module


recording_session = _load_recording_session()


# ---- Mock-Transport ----

def _make_handler(state: dict[str, Any]):
    """Erzeugt einen Handler, der die Endpunkt-Sequenz simuliert."""

    def handler(request: httpx.Request) -> httpx.Response:
        # MockTransport sieht den vollen Pfad inkl. base_url-Prefix.
        path = request.url.path
        if path.startswith("/v1/api"):
            path = path[len("/v1/api"):]
        method = request.method
        state.setdefault("calls", []).append((method, path, dict(request.url.params)))

        if path == "/iserver/auth/status":
            return httpx.Response(
                200,
                json={
                    "authenticated": state.get("authenticated", True),
                    "competing": False,
                    "connected": True,
                    "MAC": "MOCKED",
                    "userId": 123456,
                    "fail": "",
                },
            )

        if path == "/tickle" and method == "POST":
            return httpx.Response(200, json={"session": "abc", "userId": 123456})

        if path == "/iserver/secdef/search" and method == "GET":
            symbol = request.url.params.get("symbol", "").upper()
            mapping = {"AAPL": 265598, "MSFT": 272093, "SAP": 104747}
            conid = mapping.get(symbol)
            if conid is None:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[{"conid": conid, "symbol": symbol, "secType": "STK"}],
            )

        if path == "/iserver/secdef/info" and method == "GET":
            conid = int(request.url.params.get("conid", "0"))
            return httpx.Response(200, json={"conid": conid, "currency": "USD"})

        if path == "/iserver/marketdata/snapshot" and method == "GET":
            n = state.setdefault("snapshot_count", 0) + 1
            state["snapshot_count"] = n
            entries: list[dict] = []
            for cid in request.url.params.get("conids", "").split(","):
                if not cid:
                    continue
                entry: dict[str, Any] = {"conid": int(cid)}
                if n >= 2:
                    entry["31"] = "150.50"
                entries.append(entry)
            return httpx.Response(200, json=entries)

        if re.match(r"^/iserver/marketdata/\d+/unsubscribe$", path):
            return httpx.Response(200, json={"success": True})

        if re.match(r"^/portfolio/[^/]+/summary$", path):
            return httpx.Response(
                200,
                json={
                    "netliquidation": {"amount": 0.0, "currency": "USD", "isNull": False},
                    "totalcashvalue": {"amount": 0.0, "currency": "USD", "isNull": False},
                    "grosspositionvalue": {"amount": 0.0, "currency": "USD", "isNull": False},
                },
            )

        if re.match(r"^/portfolio/[^/]+/positions/[^/]+$", path):
            return httpx.Response(200, json=[])

        if re.match(r"^/portfolio/[^/]+/ledger$", path):
            return httpx.Response(200, json={"USD": {"cashbalance": 0}})

        if re.match(r"^/iserver/account/[^/]+/orders/whatif$", path) and method == "POST":
            return httpx.Response(200, json=[{"amount": {"total": "150.00"}}])

        if re.match(r"^/iserver/account/[^/]+/orders$", path) and method == "POST":
            return httpx.Response(
                200, json=[{"order_id": "1000123", "order_status": "PendingSubmit"}]
            )

        if re.match(r"^/iserver/account/orders/[^/]+$", path) and method == "GET":
            return httpx.Response(
                200, json={"order_id": "1000123", "order_status": "Cancelled"}
            )

        if re.match(r"^/iserver/account/[^/]+/order/[^/]+$", path) and method == "DELETE":
            return httpx.Response(200, json={"order_id": "1000123", "msg": "cancelled"})

        if path == "/iserver/account/trades":
            return httpx.Response(200, json=[])

        return httpx.Response(404, json={"error": "unhandled path", "path": path})

    return handler


@pytest.mark.asyncio
async def test_happy_path_records_all_endpoints(tmp_path: Path, monkeypatch) -> None:
    state: dict[str, Any] = {}
    transport = httpx.MockTransport(_make_handler(state))

    # Wir patchen httpx.AsyncClient so, dass das transport-Argument injiziert wird.
    original_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(recording_session.httpx, "AsyncClient", factory)

    args = recording_session._build_parser().parse_args([
        "happy-path",
        "--record-dir", str(tmp_path),
        "--base-url", "http://mock.invalid/v1/api",
        "--account-id", "U25235077",
        "--symbols", "AAPL", "MSFT", "SAP",
        "--yes",
    ])
    rc = await recording_session.run_happy_path(args)
    assert rc == 0

    files = sorted(p.name for p in tmp_path.glob("*.json"))
    # Manifest plus mehrere Recordings
    assert "live-recording-manifest.json" in files
    recordings = [f for f in files if f != "live-recording-manifest.json"]
    assert len(recordings) >= 12, f"erwartet >=12 recordings, hatte {len(recordings)}"

    # Manifest hat erwartete Felder
    manifest = json.loads(
        (tmp_path / "live-recording-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["scenario"] == "happy-path"
    assert manifest["account_id"] == "U25235077"
    assert manifest["symbols"] == ["AAPL", "MSFT", "SAP"]
    assert manifest["broker_gateway_version"]
    assert "recorded_at" in manifest


@pytest.mark.asyncio
async def test_happy_path_aborts_when_not_authenticated(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state: dict[str, Any] = {"authenticated": False}
    transport = httpx.MockTransport(_make_handler(state))

    original_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(recording_session.httpx, "AsyncClient", factory)

    args = recording_session._build_parser().parse_args([
        "happy-path",
        "--record-dir", str(tmp_path),
        "--base-url", "http://mock.invalid/v1/api",
        "--yes",
    ])
    rc = await recording_session.run_happy_path(args)
    assert rc == 3
    err = capsys.readouterr().err
    assert "Browser-Login" in err or "authenticated" in err
    # Keine Recordings entstehen, wenn die Voraussetzung scheitert.
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.asyncio
async def test_happy_path_skip_orders_omits_whatif(
    tmp_path: Path, monkeypatch
) -> None:
    state: dict[str, Any] = {}
    transport = httpx.MockTransport(_make_handler(state))
    original_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(recording_session.httpx, "AsyncClient", factory)

    args = recording_session._build_parser().parse_args([
        "happy-path",
        "--record-dir", str(tmp_path),
        "--base-url", "http://mock.invalid/v1/api",
        "--symbols", "AAPL",
        "--skip-orders",
        "--yes",
    ])
    rc = await recording_session.run_happy_path(args)
    assert rc == 0

    paths = {call[1] for call in state["calls"]}
    assert not any("whatif" in p for p in paths)
    assert not any(p.endswith("/orders") for p in paths if "GET" not in p)


def test_parser_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        recording_session._build_parser().parse_args([])


def _make_error_handler(state: dict[str, Any]):
    """Handler fuer Error-Path-Tests: bestimmte Pfade liefern provoziert Fehler."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/api"):
            path = path[len("/v1/api"):]
        method = request.method
        state.setdefault("calls", []).append((method, path, dict(request.url.params)))

        if path == "/iserver/auth/status":
            return httpx.Response(200, json={
                "authenticated": True, "competing": False, "connected": True,
                "MAC": "MOCKED", "userId": 1, "fail": "",
            })
        if path == "/iserver/marketdata/snapshot":
            n = state.setdefault("snap_count", 0) + 1
            state["snap_count"] = n
            if n >= 3:
                return httpx.Response(429, json={"error": "pacing-violation"})
            return httpx.Response(200, json=[])
        if path == "/iserver/secdef/info":
            cid = request.url.params.get("conid", "")
            if cid == "999999999":
                return httpx.Response(404, json={"error": "Resource not found"})
            return httpx.Response(200, json={"conid": int(cid)})
        if "orders/whatif" in path:
            return httpx.Response(400, json={"error": "Order quantity must be > 0"})
        if path.startswith("/iserver/account/order/status/"):
            return httpx.Response(404, json={"error": "Unknown order id"})
        if path == "/logout":
            return httpx.Response(200, json={"status": True})
        if path == "/reauthenticate":
            return httpx.Response(401, json={"error": "Not authenticated"})
        return httpx.Response(404, json={"error": "unhandled", "path": path})

    return handler


@pytest.mark.asyncio
async def test_error_path_records_at_least_four_cases(
    tmp_path: Path, monkeypatch
) -> None:
    state: dict[str, Any] = {}
    transport = httpx.MockTransport(_make_error_handler(state))
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(recording_session.httpx, "AsyncClient", factory)

    args = recording_session._build_parser().parse_args([
        "error-path",
        "--record-dir", str(tmp_path),
        "--base-url", "http://mock.invalid/v1/api",
        "--account-id", "U25235077",
        "--yes",
    ])
    rc = await recording_session.run_error_path(args)
    assert rc == 0

    files = sorted(p.name for p in tmp_path.glob("*.json"))
    recordings = [f for f in files if f != "live-recording-manifest.json"]
    # Pacing (429), invalid conid (404), whatif qty=0 (400), unknown order (404).
    assert len(recordings) >= 4, recordings

    # Pacing-Recording vorhanden mit Status 429.
    pacing_files = [f for f in recordings if "snapshot" in f]
    assert pacing_files, "no snapshot recording"
    pacing_envs = [
        json.loads((tmp_path / f).read_text(encoding="utf-8")) for f in pacing_files
    ]
    assert any(e["response"]["status_code"] == 429 for e in pacing_envs)


@pytest.mark.asyncio
async def test_error_path_with_reauth_fail_writes_logout_and_reauth(
    tmp_path: Path, monkeypatch
) -> None:
    state: dict[str, Any] = {}
    transport = httpx.MockTransport(_make_error_handler(state))
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(recording_session.httpx, "AsyncClient", factory)

    args = recording_session._build_parser().parse_args([
        "error-path",
        "--record-dir", str(tmp_path),
        "--base-url", "http://mock.invalid/v1/api",
        "--with-reauth-fail",
        "--yes",
    ])
    rc = await recording_session.run_error_path(args)
    assert rc == 0
    paths = {call[1] for call in state["calls"]}
    assert "/logout" in paths
    assert "/reauthenticate" in paths
