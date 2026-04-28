"""Tests fuer ``scripts/check_mock_drift.py`` (AP-02 #06).

Verifiziert die End-to-End-Logik mit MockTransport: Fixture im tmp_path,
Mock-Antwort konstruiert, ``run_drift_check`` aufgerufen, Klassifikation
und Exit-Code geprueft.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

# Skript wird ueber Path-Manipulation importiert, weil scripts/ kein Paket ist.
import importlib.util
import sys

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_mock_drift.py"
_spec = importlib.util.spec_from_file_location("check_mock_drift", _SCRIPT)
assert _spec and _spec.loader
check_mock_drift = importlib.util.module_from_spec(_spec)
sys.modules["check_mock_drift"] = check_mock_drift
_spec.loader.exec_module(check_mock_drift)  # type: ignore[union-attr]


def _write_fixture(directory: Path, name: str, *, request: dict, response: dict) -> Path:
    envelope = {
        "request": request,
        "response": response,
        "recorded_at": "2026-04-25T12:00:00+00:00",
        "normalized": True,
    }
    target = directory / name
    target.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    return target


def _mock_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(
        base_url="http://mock-cpgateway/v1/api",
        transport=transport,
        timeout=5.0,
    )


@pytest.mark.asyncio
async def test_no_drift_when_live_matches_fixture(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "live"
    fixtures_dir.mkdir()
    _write_fixture(
        fixtures_dir, "iserver_auth_status__GET__noquery_01.json",
        request={"method": "GET", "url": "/iserver/auth/status", "query": {}, "body_json": None},
        response={
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body_json": {"authenticated": True, "established": True, "competing": False},
            "body_text": None,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"authenticated": True, "established": True, "competing": False}
        )

    async with _mock_client(handler) as client:
        results = await check_mock_drift.run_drift_check(
            client, [fixtures_dir / "iserver_auth_status__GET__noquery_01.json"]
        )

    assert len(results) == 1
    assert results[0].report is not None
    assert results[0].report.is_clean()


@pytest.mark.asyncio
async def test_additive_drift_returns_exit_zero(tmp_path: Path) -> None:
    """Verification 5: nur additive Drift -> Exit 0."""
    fixtures_dir = tmp_path / "live"
    fixtures_dir.mkdir()
    fixture = _write_fixture(
        fixtures_dir, "iserver_accounts__GET__noquery_01.json",
        request={"method": "GET", "url": "/iserver/accounts", "query": {}, "body_json": None},
        response={
            "status_code": 200, "headers": {"content-type": "application/json"},
            "body_json": {"accounts": ["U1"]}, "body_text": None,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # Live hat ein zusaetzliches Feld - additive Drift.
        return httpx.Response(200, json={"accounts": ["U1"], "selectedAccount": "U1"})

    async with _mock_client(handler) as client:
        results = await check_mock_drift.run_drift_check(client, [fixture])

    assert results[0].report is not None
    assert results[0].report.is_minor()
    summary = check_mock_drift._summarize(results)
    assert summary["minor"] == 1
    assert summary["breaking"] == 0


@pytest.mark.asyncio
async def test_breaking_drift_classified_correctly(tmp_path: Path) -> None:
    """Verification 4: kuenstlich verfaelschte Fixture -> breaking + Exit 1."""
    fixtures_dir = tmp_path / "live"
    fixtures_dir.mkdir()
    fixture = _write_fixture(
        fixtures_dir, "iserver_accounts__GET__noquery_01.json",
        request={"method": "GET", "url": "/iserver/accounts", "query": {}, "body_json": None},
        response={
            "status_code": 200, "headers": {"content-type": "application/json"},
            "body_json": {"accounts": ["U1"], "kritischesFeld": "muss-da-sein"},
            "body_text": None,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # Live hat kritischesFeld nicht -> Feld entfernt -> breaking.
        return httpx.Response(200, json={"accounts": ["U1"]})

    async with _mock_client(handler) as client:
        results = await check_mock_drift.run_drift_check(client, [fixture])

    assert results[0].report is not None
    assert results[0].report.is_breaking()
    summary = check_mock_drift._summarize(results)
    assert summary["breaking"] == 1


@pytest.mark.asyncio
async def test_status_code_change_is_breaking(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "live"
    fixtures_dir.mkdir()
    fixture = _write_fixture(
        fixtures_dir, "iserver_accounts__GET__noquery_01.json",
        request={"method": "GET", "url": "/iserver/accounts", "query": {}, "body_json": None},
        response={
            "status_code": 200, "headers": {"content-type": "application/json"},
            "body_json": {"accounts": ["U1"]}, "body_text": None,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "Service Unavailable"})

    async with _mock_client(handler) as client:
        results = await check_mock_drift.run_drift_check(client, [fixture])

    assert results[0].report is not None
    assert results[0].report.is_breaking()
    assert any(c.path == "<http_status>" for c in results[0].report.changed_types)


@pytest.mark.asyncio
async def test_order_endpoint_is_skipped(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "live"
    fixtures_dir.mkdir()
    # Whatif-Endpoint -> muss uebersprungen werden (Side Effects + dokumentarisch).
    fixture = _write_fixture(
        fixtures_dir, "iserver_account_U25235077_orders_whatif__POST__noquery_01.json",
        request={
            "method": "POST", "url": "/iserver/account/U25235077/orders/whatif",
            "query": {}, "body_json": {"orders": []},
        },
        response={
            "status_code": 200, "headers": {"content-type": "application/json"},
            "body_json": {"amount": "0"}, "body_text": None,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Order-Endpunkt darf nicht angefragt werden")

    async with _mock_client(handler) as client:
        results = await check_mock_drift.run_drift_check(client, [fixture])

    assert results[0].report is None
    assert "order" in (results[0].skip_reason or "")


@pytest.mark.asyncio
async def test_recorded_4xx_5xx_fixture_skipped(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "live"
    fixtures_dir.mkdir()
    # 503-Recording (z.B. aus errors/) ist dokumentarisch und wird nicht
    # erneut abgespielt - der Endpunkt koennte mittlerweile 200 liefern.
    fixture = _write_fixture(
        fixtures_dir, "iserver_secdef_info__GET__noquery_01.json",
        request={"method": "GET", "url": "/iserver/secdef/info", "query": {"conid": "999999"}, "body_json": None},
        response={
            "status_code": 503, "headers": {"content-type": "application/json"},
            "body_json": {"error": "Service Unavailable"}, "body_text": None,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("4xx/5xx-Fixture darf nicht erneut abgefragt werden")

    async with _mock_client(handler) as client:
        results = await check_mock_drift.run_drift_check(client, [fixture])

    assert results[0].report is None
    assert "4xx" in (results[0].skip_reason or "") or "5xx" in (results[0].skip_reason or "")


@pytest.mark.asyncio
async def test_value_drift_does_not_trigger_breaking(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "live"
    fixtures_dir.mkdir()
    fixture = _write_fixture(
        fixtures_dir, "iserver_accounts__GET__noquery_01.json",
        request={"method": "GET", "url": "/iserver/accounts", "query": {}, "body_json": None},
        response={
            "status_code": 200, "headers": {"content-type": "application/json"},
            "body_json": {"accounts": ["U1"], "msgVersion": "1"}, "body_text": None,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accounts": ["U1"], "msgVersion": "2"})

    async with _mock_client(handler) as client:
        results = await check_mock_drift.run_drift_check(client, [fixture])

    assert results[0].report is not None
    assert not results[0].report.is_breaking()
    assert results[0].report.classification == "value drift"


def test_report_renders_markdown(tmp_path: Path) -> None:
    from tests.cp_mock.diff import diff_recording

    results = [
        check_mock_drift.DriftResult(
            fixture=Path("foo.json"), path="/foo", method="GET",
            report=diff_recording({"a": 1}, {"a": 1}), skip_reason=None,
        ),
        check_mock_drift.DriftResult(
            fixture=Path("bar.json"), path="/bar", method="GET",
            report=diff_recording({"a": 1}, {"a": 1, "b": 2}), skip_reason=None,
        ),
        check_mock_drift.DriftResult(
            fixture=Path("baz.json"), path="/baz", method="POST",
            report=None, skip_reason="order/session-endpoint",
        ),
    ]
    target = check_mock_drift._write_report(tmp_path, date(2026, 4, 25), results)

    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "# Drift-Bericht 2026-04-25" in text
    assert "## Zusammenfassung" in text
    assert "GET /foo" in text
    assert "GET /bar" in text
    assert "POST /baz" in text
    assert "uebersprungen" in text


def test_summary_counts_match_classifications(tmp_path: Path) -> None:
    from tests.cp_mock.diff import diff_recording

    results = [
        check_mock_drift.DriftResult(
            fixture=Path("a"), path="/a", method="GET",
            report=diff_recording({"a": 1}, {"a": 1}), skip_reason=None,
        ),
        check_mock_drift.DriftResult(
            fixture=Path("b"), path="/b", method="GET",
            report=diff_recording({"a": 1, "b": 2}, {"a": 1}), skip_reason=None,
        ),
        check_mock_drift.DriftResult(
            fixture=Path("c"), path="/c", method="GET",
            report=diff_recording({"a": 1}, {"a": 1, "b": 2}), skip_reason=None,
        ),
        check_mock_drift.DriftResult(
            fixture=Path("d"), path="/d", method="GET",
            report=diff_recording({"x": "neu"}, {"x": "alt"}), skip_reason=None,
        ),
        check_mock_drift.DriftResult(
            fixture=Path("e"), path="/e", method="POST",
            report=None, skip_reason="skip",
        ),
    ]
    summary = check_mock_drift._summarize(results)
    assert summary == {"no": 1, "minor": 1, "value": 1, "breaking": 1, "skipped": 1}


def test_skip_logic_blocks_orders_and_logout() -> None:
    assert check_mock_drift._should_skip("/iserver/account/U1/orders/whatif", "POST")
    assert check_mock_drift._should_skip("/iserver/account/orders/123", "GET")
    assert check_mock_drift._should_skip("/iserver/account/U1/order/456", "DELETE")
    assert check_mock_drift._should_skip("/logout", "POST")
    assert check_mock_drift._should_skip("/reauthenticate", "POST")
    # POST auf Tickle ist erlaubt.
    assert not check_mock_drift._should_skip("/tickle", "POST")
    # GET auf normale Endpunkte natuerlich erlaubt.
    assert not check_mock_drift._should_skip("/iserver/auth/status", "GET")
    assert not check_mock_drift._should_skip("/portfolio/U1/summary", "GET")


def test_build_acceptance_report_path_uses_commit_sha(tmp_path: Path) -> None:
    """AP-03: Build-Acceptance-Bericht heisst build-<sha>.md statt Datums-Datei."""
    from tests.cp_mock.diff import diff_recording

    results = [
        check_mock_drift.DriftResult(
            fixture=Path("a"), path="/a", method="GET",
            report=diff_recording({"a": 1}, {"a": 1}), skip_reason=None,
        ),
    ]
    target = check_mock_drift._write_report(
        tmp_path, date(2026, 4, 28), results,
        build_acceptance=True,
        commit_sha="abcdef0123456789beef",
    )
    assert target.name == "build-abcdef012345.md"
    assert target.exists()


def test_build_acceptance_report_path_unknown_sha(tmp_path: Path) -> None:
    from tests.cp_mock.diff import diff_recording

    results = [
        check_mock_drift.DriftResult(
            fixture=Path("a"), path="/a", method="GET",
            report=diff_recording({"a": 1}, {"a": 1}), skip_reason=None,
        ),
    ]
    target = check_mock_drift._write_report(
        tmp_path, date(2026, 4, 28), results,
        build_acceptance=True,
        commit_sha=None,
    )
    assert target.name == "build-unknown.md"


def test_resolve_warmup_defaults() -> None:
    """Warmup-Logik: ohne Flag = 0; mit --build-acceptance = 90; explizit = wins."""
    parser = check_mock_drift._build_parser()

    args = parser.parse_args([])
    assert check_mock_drift._resolve_warmup(args) == 0

    args = parser.parse_args(["--build-acceptance"])
    assert check_mock_drift._resolve_warmup(args) == 90

    args = parser.parse_args(["--build-acceptance", "--warmup-seconds", "10"])
    assert check_mock_drift._resolve_warmup(args) == 10

    args = parser.parse_args(["--build-acceptance", "--warmup-seconds", "0"])
    assert check_mock_drift._resolve_warmup(args) == 0


def test_resolve_commit_sha_from_args(monkeypatch) -> None:
    parser = check_mock_drift._build_parser()
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("CI_COMMIT_SHA", raising=False)

    args = parser.parse_args(["--build-acceptance", "--commit-sha", "abc123"])
    assert check_mock_drift._resolve_commit_sha(args) == "abc123"

    args = parser.parse_args(["--build-acceptance"])
    assert check_mock_drift._resolve_commit_sha(args) == "unknown"

    monkeypatch.setenv("GIT_COMMIT", "from-env")
    args = parser.parse_args(["--build-acceptance"])
    assert check_mock_drift._resolve_commit_sha(args) == "from-env"
