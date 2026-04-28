"""Tests für die cp_gateway_mock-Fixture.

Demonstriert gleichzeitig die Nutzung für Folge-Karten: man instanziiert
einen httpx.Client gegen `mock.base_url` und ruft die Endpunkte ab. Flags
am Mock-Objekt steuern Abweichungen vom happy path.
"""
from __future__ import annotations

import httpx
import pytest


def test_auth_status_authenticated(cp_gateway_mock) -> None:
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        response = client.get("/iserver/auth/status")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["connected"] is True


def test_auth_lost_flag_flips_authenticated(cp_gateway_mock) -> None:
    cp_gateway_mock.auth_lost = True
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        response = client.get("/iserver/auth/status")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is False
    assert body["fail"] == "session-lost"


def test_tickle_returns_session(cp_gateway_mock) -> None:
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        response = client.post("/tickle")
    assert response.status_code == 200
    body = response.json()
    # session-ID-Wert ist normalisiert (live: "<SESSION_ID>") oder seed-konkret
    # ("mock-session-id"), abhaengig von der aktiven Recording-Quelle.
    assert isinstance(body["session"], str) and body["session"]
    assert body["userId"]
    assert body["iserver"]["authStatus"]["authenticated"] is True


def test_secdef_search_returns_known_symbols(cp_gateway_mock) -> None:
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        response = client.get("/iserver/secdef/search", params={"symbol": "AAPL"})
    assert response.status_code == 200
    body = response.json()
    # CP-Gateway liefert in Realitaet mehrere Listings pro Symbol
    # (NASDAQ, TSE, MEXI, EBS, BOND); seed liefert nur 1. Test akzeptiert
    # beides und prueft, dass das primary listing AAPL/conid=265598 ist.
    assert len(body) >= 1
    primary = body[0]
    assert primary["symbol"] == "AAPL"
    assert int(primary["conid"]) == 265598


def test_secdef_search_unknown_symbol_returns_empty(cp_gateway_mock) -> None:
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        response = client.get("/iserver/secdef/search", params={"symbol": "ZZZZ"})
    assert response.status_code == 200
    assert response.json() == []


def test_snapshot_first_call_prime_then_values(cp_gateway_mock) -> None:
    params = {"conids": "265598", "fields": "31,84,86,6509"}
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        first = client.get("/iserver/marketdata/snapshot", params=params).json()
        second = client.get("/iserver/marketdata/snapshot", params=params).json()
    # First-Call: nur conid, kein Last/Bid/Ask.
    assert first[0]["conid"] == 265598
    assert "31" not in first[0]
    # Second-Call: Werte sind primed.
    assert second[0]["conid"] == 265598
    assert second[0]["31"] == "150.50"
    assert second[0]["84"] == "150.45"
    assert second[0]["86"] == "150.55"
    assert second[0]["6509"] == "DPB"


def test_snapshot_tracks_subscriptions(cp_gateway_mock) -> None:
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        client.get("/iserver/marketdata/snapshot", params={"conids": "265598,272093", "fields": "31"})
    assert cp_gateway_mock.subscriptions == {265598, 272093}


def test_unsubscribe_removes_from_subscriptions(cp_gateway_mock) -> None:
    cp_gateway_mock.subscriptions = {265598, 272093}
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        response = client.get("/iserver/marketdata/265598/unsubscribe")
    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert cp_gateway_mock.subscriptions == {272093}


def test_order_create_then_status_lifecycle(cp_gateway_mock) -> None:
    account_id = "U25235077"
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        place = client.post(
            f"/iserver/account/{account_id}/orders",
            json={"orders": [{"conid": 265598, "side": "BUY", "quantity": 1}]},
        )
        assert place.status_code == 200
        order_id = place.json()[0]["order_id"]
        assert order_id in cp_gateway_mock.orders

        status_1 = client.get(f"/iserver/account/order/status/{order_id}").json()
        status_2 = client.get(f"/iserver/account/order/status/{order_id}").json()
        status_3 = client.get(f"/iserver/account/order/status/{order_id}").json()
    assert status_1["order_status"] == "Submitted"
    assert status_2["order_status"] == "Filled"
    assert status_3["order_status"] == "Filled"


def test_order_cancel_marks_cancelled(cp_gateway_mock) -> None:
    account_id = "U25235077"
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        order_id = client.post(f"/iserver/account/{account_id}/orders", json={}).json()[0]["order_id"]
        cancel = client.delete(f"/iserver/account/{account_id}/order/{order_id}")
    assert cancel.status_code == 200
    assert cp_gateway_mock.orders[order_id]["status"] == "Cancelled"


def test_portfolio_and_ledger_return_account_data(cp_gateway_mock) -> None:
    account_id = "U25235077"
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        positions = client.get(f"/portfolio/{account_id}/positions/0").json()
        ledger = client.get(f"/portfolio/{account_id}/ledger").json()
        summary = client.get(f"/portfolio/{account_id}/summary").json()
    assert isinstance(positions, list) and len(positions) >= 1
    assert any("conid" in p for p in positions)
    # Ledger hat mindestens eine Currency neben dem (verworfenen) BASE-Aggregat.
    real_currencies = [k for k in ledger if k.upper() != "BASE"]
    assert real_currencies, "ledger sollte mindestens eine Currency liefern"
    sample = ledger[real_currencies[0]]
    assert "cashbalance" in sample
    # Summary liefert das IBKR-Schema mit netliquidation als amount-currency-Objekt.
    assert "netliquidation" in summary
    assert "amount" in summary["netliquidation"] and "currency" in summary["netliquidation"]


def test_trades_returns_deterministic_list(cp_gateway_mock) -> None:
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        trades = client.get("/iserver/account/trades", params={"days": "3"}).json()
    assert len(trades) == 3
    assert trades[0]["execution_id"] == "exec-000"


def test_pacing_violation_after_n(cp_gateway_mock) -> None:
    cp_gateway_mock.pacing_violation_after_n = 2
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        assert client.get("/iserver/auth/status").status_code == 200
        assert client.get("/iserver/auth/status").status_code == 200
        third = client.get("/iserver/auth/status")
    assert third.status_code == 429
    assert third.json() == {"error": "pacing-violation"}


def test_reauthenticate_returns_triggered(cp_gateway_mock) -> None:
    with httpx.Client(base_url=cp_gateway_mock.base_url) as client:
        response = client.post("/reauthenticate")
    assert response.status_code == 200
    assert response.json() == {"message": "triggered"}


# ---- Replay-Loader-Tests (AP-02 #03) ----

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from tests.cp_mock import RecordingNotFoundError, load_recording  # noqa: E402


def _write_recording(path: Path, body: dict, status_code: int = 200) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "request": {"method": "GET", "url": "/x", "query": {}, "headers": {}, "body_json": None, "body_text": None},
        "response": {"status_code": status_code, "headers": {}, "body_json": body, "body_text": None},
        "recorded_at": "2026-04-25T00:00:00+00:00",
        "normalized": False,
        "source": "test",
    }
    path.write_text(json.dumps(envelope), encoding="utf-8")


def test_replay_loader_picks_correct_recording() -> None:
    """Default-Loader liefert das passende Recording fuer
    /iserver/auth/status (live > seed)."""
    response = load_recording("/iserver/auth/status", method="GET")
    assert response["status_code"] == 200
    body = response["body_json"]
    assert body["authenticated"] is True
    # MAC ist im seed "MOCKED", in live die echte Hardware-MAC. Pruefen,
    # dass das Feld vorhanden und nicht leer ist.
    assert isinstance(body.get("MAC"), str) and body["MAC"]


def test_replay_loader_first_call_prime(tmp_path: Path) -> None:
    """Zwei aufeinanderfolgende Aufrufe (call_index 1 und 2) liefern
    _01.json bzw. _02.json - das Suffix-Schema des Recorders."""
    seed = tmp_path / "seed"
    _write_recording(
        seed / "iserver_marketdata_snapshot__GET__noquery_01.json",
        body=[{"conid": 265598, "_updated": 1}],
    )
    _write_recording(
        seed / "iserver_marketdata_snapshot__GET__noquery_02.json",
        body=[{"conid": 265598, "_updated": 2, "31": "150.50"}],
    )

    first = load_recording(
        "/iserver/marketdata/snapshot", method="GET", call_index=1, base_dir=tmp_path
    )
    second = load_recording(
        "/iserver/marketdata/snapshot", method="GET", call_index=2, base_dir=tmp_path
    )
    assert "31" not in first["body_json"][0], "first call should be the prime (no value)"
    assert second["body_json"][0]["31"] == "150.50"


def test_replay_loader_falls_back_to_seed_when_no_live_recording(tmp_path: Path) -> None:
    """Sobald ein live-Recording existiert, hat es Vorrang. Wenn nicht,
    wird das seed-File genommen."""
    seed = tmp_path / "seed"
    _write_recording(
        seed / "iserver_auth_status__GET__noquery_01.json",
        body={"authenticated": True, "source": "from-seed"},
    )

    # Erst nur seed -> seed-Body kommt zurueck.
    response = load_recording("/iserver/auth/status", method="GET", base_dir=tmp_path)
    assert response["body_json"]["source"] == "from-seed"

    # Jetzt zusaetzlich live -> live ueberschreibt seed.
    live = tmp_path / "live"
    _write_recording(
        live / "iserver_auth_status__GET__noquery_01.json",
        body={"authenticated": True, "source": "from-live"},
    )
    response = load_recording("/iserver/auth/status", method="GET", base_dir=tmp_path)
    assert response["body_json"]["source"] == "from-live"


def test_replay_loader_raises_when_no_recording_found(tmp_path: Path) -> None:
    """Sauberer Fehler wenn weder live noch seed das Recording haben."""
    with pytest.raises(RecordingNotFoundError):
        load_recording("/nicht/vorhanden", method="GET", base_dir=tmp_path)
