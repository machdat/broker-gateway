"""Integrationstests fuer ``scripts/check_doc_drift.py`` (AP-03).

Verifiziert die End-to-End-Logik mit ``httpx.MockTransport``: Baseline
in tmp_path, Mock-HTTP fuer Live-Spec, ``run`` aufgerufen, Exit-Code +
Bericht + Auto-Karten-Anlage geprueft.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable

import httpx


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_doc_drift.py"
_spec = importlib.util.spec_from_file_location("check_doc_drift", _SCRIPT)
assert _spec and _spec.loader
check_doc_drift = importlib.util.module_from_spec(_spec)
sys.modules["check_doc_drift"] = check_doc_drift
_spec.loader.exec_module(check_doc_drift)  # type: ignore[union-attr]


# --- Hilfen --------------------------------------------------------------


def _minimal_spec(*, response_props: dict[str, dict] | None = None) -> dict[str, Any]:
    return {
        "swagger": "2.0",
        "info": {"title": "IBKR-Mock", "version": "1.0"},
        "paths": {
            "/x": {
                "get": {
                    "summary": "x",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "schema": {
                                "type": "object",
                                "properties": response_props
                                or {"a": {"type": "string"}},
                            },
                        }
                    },
                }
            }
        },
    }


def _write_baseline(path: Path, spec: dict[str, Any]) -> Path:
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


class _ClientFactory:
    """Liefert MockTransport-Clients und kann mehrere Endpunkt-Handler routen.

    Wird als ``http_client_factory`` an ``run`` uebergeben. Der MockTransport
    unterscheidet anhand der absoluten URL: Spec-Quelle vs. KanPrompt.
    """

    def __init__(self) -> None:
        self.handlers: list[Callable[[httpx.Request], httpx.Response]] = []
        self.calls: list[httpx.Request] = []

    def add(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handlers.append(handler)

    def __call__(self) -> httpx.Client:
        def _route(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            for h in self.handlers:
                resp = h(request)
                if resp is not None:
                    return resp
            return httpx.Response(404, json={"error": "no handler"})

        return httpx.Client(transport=httpx.MockTransport(_route), follow_redirects=True)


def _spec_handler(spec: dict[str, Any], url_substring: str = "interactivebrokers.com") -> Callable:
    def handler(request: httpx.Request) -> httpx.Response | None:
        if url_substring in str(request.url):
            return httpx.Response(200, json=spec)
        return None
    return handler


def _kanprompt_list_handler(existing_titles: list[str]) -> Callable:
    """GET /api/v1/projects/.../cards -> Liste mit den gegebenen Titeln."""
    def handler(request: httpx.Request) -> httpx.Response | None:
        if request.method == "GET" and "/cards" in str(request.url) and request.url.path.endswith("/cards"):
            return httpx.Response(
                200,
                json=[{"title": t} for t in existing_titles],
            )
        return None
    return handler


def _kanprompt_create_handler(captured: list[dict[str, Any]]) -> Callable:
    """POST /api/v1/projects/.../cards -> Karte angelegt."""
    def handler(request: httpx.Request) -> httpx.Response | None:
        if request.method == "POST" and request.url.path.endswith("/cards"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(201, json={"id": "test-card-id"})
        return None
    return handler


# --- Tests ----------------------------------------------------------------


def test_run_no_drift_returns_exit_zero(tmp_path: Path) -> None:
    spec = _minimal_spec()
    baseline = _write_baseline(tmp_path / "baseline.json", spec)

    factory = _ClientFactory()
    factory.add(_spec_handler(spec))

    outcome = check_doc_drift.run(
        source_url="https://www.interactivebrokers.com/api/doc.json",
        baseline_path=baseline,
        report_dir=tmp_path / "reports",
        today=date(2026, 4, 28),
        auto_card=False,
        kanprompt_base_url="http://mock-kanprompt:8000",
        project_id="PROJ",
        http_client_factory=factory,
    )

    assert outcome.exit_code == check_doc_drift.EXIT_OK
    assert outcome.classification == "no drift"
    assert outcome.report_path is not None and outcome.report_path.is_file()
    assert "Klassifikation" in outcome.report_path.read_text(encoding="utf-8")


def test_run_minor_drift_returns_exit_two(tmp_path: Path) -> None:
    expected = _minimal_spec(response_props={"a": {"type": "string"}})
    actual = _minimal_spec(response_props={
        "a": {"type": "string"},
        "b": {"type": "integer"},
    })
    baseline = _write_baseline(tmp_path / "baseline.json", expected)

    factory = _ClientFactory()
    factory.add(_spec_handler(actual))

    outcome = check_doc_drift.run(
        source_url="https://www.interactivebrokers.com/api/doc.json",
        baseline_path=baseline,
        report_dir=tmp_path / "reports",
        today=date(2026, 4, 28),
        auto_card=False,
        kanprompt_base_url="http://mock-kanprompt:8000",
        project_id="PROJ",
        http_client_factory=factory,
    )

    assert outcome.exit_code == check_doc_drift.EXIT_MINOR
    assert outcome.classification == "minor (additive)"


def test_run_breaking_drift_returns_exit_one(tmp_path: Path) -> None:
    expected = _minimal_spec(response_props={
        "a": {"type": "string"},
        "b": {"type": "integer"},
    })
    actual = _minimal_spec(response_props={"a": {"type": "string"}})
    baseline = _write_baseline(tmp_path / "baseline.json", expected)

    factory = _ClientFactory()
    factory.add(_spec_handler(actual))

    outcome = check_doc_drift.run(
        source_url="https://www.interactivebrokers.com/api/doc.json",
        baseline_path=baseline,
        report_dir=tmp_path / "reports",
        today=date(2026, 4, 28),
        auto_card=False,
        kanprompt_base_url="http://mock-kanprompt:8000",
        project_id="PROJ",
        http_client_factory=factory,
    )

    assert outcome.exit_code == check_doc_drift.EXIT_BREAKING
    assert outcome.classification == "breaking"


def test_run_unreachable_source_returns_exit_three(tmp_path: Path) -> None:
    spec = _minimal_spec()
    baseline = _write_baseline(tmp_path / "baseline.json", spec)

    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    factory = _ClientFactory()
    factory.add(fail_handler)

    try:
        check_doc_drift.run(
            source_url="https://www.interactivebrokers.com/api/doc.json",
            baseline_path=baseline,
            report_dir=tmp_path / "reports",
            today=date(2026, 4, 28),
            auto_card=False,
            kanprompt_base_url="http://mock-kanprompt:8000",
            project_id="PROJ",
            http_client_factory=factory,
        )
    except check_doc_drift.DocDriftError:
        pass
    else:
        raise AssertionError("Erwartete DocDriftError bei 503-Quelle.")


def test_main_returns_exit_three_on_unreachable(tmp_path: Path, monkeypatch) -> None:
    """Smoke-Test fuer main(): wandelt DocDriftError in Exit-Code 3."""
    spec = _minimal_spec()
    baseline = _write_baseline(tmp_path / "baseline.json", spec)

    factory = _ClientFactory()
    factory.add(lambda req: httpx.Response(503, json={"error": "down"}))

    # main() ruft _default_client_factory direkt - wir patchen das Modul.
    monkeypatch.setattr(check_doc_drift, "_default_client_factory", factory)

    code = check_doc_drift.main([
        "--source-url", "https://www.interactivebrokers.com/api/doc.json",
        "--baseline", str(baseline),
        "--report-dir", str(tmp_path / "reports"),
        "--date", "2026-04-28",
    ])

    assert code == check_doc_drift.EXIT_UNREACHABLE


def test_auto_card_creates_card_on_breaking(
    tmp_path: Path, monkeypatch
) -> None:
    expected = _minimal_spec(response_props={
        "a": {"type": "string"},
        "b": {"type": "integer"},
    })
    actual = _minimal_spec(response_props={"a": {"type": "string"}})
    baseline = _write_baseline(tmp_path / "baseline.json", expected)

    factory = _ClientFactory()
    factory.add(_spec_handler(actual))
    factory.add(_kanprompt_list_handler(existing_titles=[]))
    captured_posts: list[dict[str, Any]] = []
    factory.add(_kanprompt_create_handler(captured_posts))

    monkeypatch.setenv("KANPROMPT_API_KEY", "test-key")

    outcome = check_doc_drift.run(
        source_url="https://www.interactivebrokers.com/api/doc.json",
        baseline_path=baseline,
        report_dir=tmp_path / "reports",
        today=date(2026, 4, 28),
        auto_card=True,
        kanprompt_base_url="http://mock-kanprompt:8000",
        project_id="PROJ",
        http_client_factory=factory,
    )

    assert outcome.exit_code == check_doc_drift.EXIT_BREAKING
    assert outcome.card_created is True
    assert len(captured_posts) == 1
    body = captured_posts[0]
    assert body["title"].startswith("Doku-Drift breaking 2026-04-28")
    assert body["blocked"] is True
    assert body["card_type"] == "bugfix"
    assert "ibkr-cpapi-doc.json" in body["affected_files"]


def test_auto_card_creates_card_on_minor(
    tmp_path: Path, monkeypatch
) -> None:
    expected = _minimal_spec(response_props={"a": {"type": "string"}})
    actual = _minimal_spec(response_props={
        "a": {"type": "string"},
        "b": {"type": "integer"},
    })
    baseline = _write_baseline(tmp_path / "baseline.json", expected)

    factory = _ClientFactory()
    factory.add(_spec_handler(actual))
    factory.add(_kanprompt_list_handler(existing_titles=[]))
    captured_posts: list[dict[str, Any]] = []
    factory.add(_kanprompt_create_handler(captured_posts))

    monkeypatch.setenv("KANPROMPT_API_KEY", "test-key")

    outcome = check_doc_drift.run(
        source_url="https://www.interactivebrokers.com/api/doc.json",
        baseline_path=baseline,
        report_dir=tmp_path / "reports",
        today=date(2026, 4, 28),
        auto_card=True,
        kanprompt_base_url="http://mock-kanprompt:8000",
        project_id="PROJ",
        http_client_factory=factory,
    )

    assert outcome.exit_code == check_doc_drift.EXIT_MINOR
    assert outcome.card_created is True
    assert len(captured_posts) == 1
    body = captured_posts[0]
    assert body["title"].startswith("Doku-Drift minor 2026-04-28")
    assert body["blocked"] is False
    assert body["card_type"] == "feature"


def test_auto_card_spam_protection_blocks_second_card_same_day(
    tmp_path: Path, monkeypatch
) -> None:
    expected = _minimal_spec(response_props={"a": {"type": "string"}})
    actual = _minimal_spec(response_props={
        "a": {"type": "string"},
        "b": {"type": "integer"},
    })
    baseline = _write_baseline(tmp_path / "baseline.json", expected)

    # Erste Karte angelegt - laut Mock existiert sie bereits.
    existing = ["Doku-Drift minor 2026-04-28: IBKR-Spec-Drift festgestellt"]
    factory = _ClientFactory()
    factory.add(_spec_handler(actual))
    factory.add(_kanprompt_list_handler(existing_titles=existing))
    captured_posts: list[dict[str, Any]] = []
    factory.add(_kanprompt_create_handler(captured_posts))

    monkeypatch.setenv("KANPROMPT_API_KEY", "test-key")

    outcome = check_doc_drift.run(
        source_url="https://www.interactivebrokers.com/api/doc.json",
        baseline_path=baseline,
        report_dir=tmp_path / "reports",
        today=date(2026, 4, 28),
        auto_card=True,
        kanprompt_base_url="http://mock-kanprompt:8000",
        project_id="PROJ",
        http_client_factory=factory,
    )

    assert outcome.exit_code == check_doc_drift.EXIT_MINOR
    assert outcome.card_created is False
    assert captured_posts == []


def test_auto_card_skipped_on_no_drift(
    tmp_path: Path, monkeypatch
) -> None:
    spec = _minimal_spec()
    baseline = _write_baseline(tmp_path / "baseline.json", spec)

    factory = _ClientFactory()
    factory.add(_spec_handler(spec))
    factory.add(_kanprompt_list_handler(existing_titles=[]))
    captured_posts: list[dict[str, Any]] = []
    factory.add(_kanprompt_create_handler(captured_posts))

    monkeypatch.setenv("KANPROMPT_API_KEY", "test-key")

    outcome = check_doc_drift.run(
        source_url="https://www.interactivebrokers.com/api/doc.json",
        baseline_path=baseline,
        report_dir=tmp_path / "reports",
        today=date(2026, 4, 28),
        auto_card=True,
        kanprompt_base_url="http://mock-kanprompt:8000",
        project_id="PROJ",
        http_client_factory=factory,
    )

    assert outcome.exit_code == check_doc_drift.EXIT_OK
    assert outcome.card_created is False
    assert captured_posts == []


def test_auto_card_without_api_key_warns_but_returns_exit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    expected = _minimal_spec(response_props={"a": {"type": "string"}})
    actual = _minimal_spec(response_props={
        "a": {"type": "string"},
        "b": {"type": "integer"},
    })
    baseline = _write_baseline(tmp_path / "baseline.json", expected)

    factory = _ClientFactory()
    factory.add(_spec_handler(actual))

    monkeypatch.delenv("KANPROMPT_API_KEY", raising=False)

    outcome = check_doc_drift.run(
        source_url="https://www.interactivebrokers.com/api/doc.json",
        baseline_path=baseline,
        report_dir=tmp_path / "reports",
        today=date(2026, 4, 28),
        auto_card=True,
        kanprompt_base_url="http://mock-kanprompt:8000",
        project_id="PROJ",
        http_client_factory=factory,
    )

    assert outcome.exit_code == check_doc_drift.EXIT_MINOR
    assert outcome.card_created is False
    err = capsys.readouterr().err
    assert "KANPROMPT_API_KEY" in err


def test_run_writes_report_with_breaking_section(tmp_path: Path) -> None:
    expected = _minimal_spec(response_props={
        "a": {"type": "string"}, "b": {"type": "integer"},
    })
    actual = _minimal_spec(response_props={"a": {"type": "string"}})
    baseline = _write_baseline(tmp_path / "baseline.json", expected)

    factory = _ClientFactory()
    factory.add(_spec_handler(actual))

    outcome = check_doc_drift.run(
        source_url="https://www.interactivebrokers.com/api/doc.json",
        baseline_path=baseline,
        report_dir=tmp_path / "reports",
        today=date(2026, 4, 28),
        auto_card=False,
        kanprompt_base_url="http://mock-kanprompt:8000",
        project_id="PROJ",
        http_client_factory=factory,
    )

    assert outcome.report_path is not None
    content = outcome.report_path.read_text(encoding="utf-8")
    assert "## Breaking" in content
    assert "removed_field" in content
    assert ".properties.b" in content
