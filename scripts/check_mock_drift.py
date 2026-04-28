#!/usr/bin/env python3
"""Mock-Drift-Check: Live-CP-Gateway gegen aufgezeichnete Fixtures (AP-02 #06).

Iteriert ueber ``tests/fixtures/recorded/live/`` (Order-Endpunkte
ausgeschlossen), wiederholt jeden Request gegen das laufende
CP-Gateway und vergleicht die Antwort gegen die Fixture mittels
:func:`tests.cp_mock.diff.diff_recording`.

Exit-Code:

- ``0`` - kein Schema-Bruch (no/minor/value drift toleriert).
- ``1`` - mindestens ein Endpunkt zeigt **breaking drift** (entfernte
  Felder oder Typaenderungen) - sofortige Eskalation noetig.
- ``2`` - kein Recording vorhanden / I/O-Fehler.
- ``3`` - Auth-Status zeigt ``authenticated=false`` (Login fehlt).

Schreibt einen Markdown-Bericht nach ``reports/drift/<YYYY-MM-DD>.md``
(eingecheckt - bildet den Drift-Verlauf des Repos ab). Der Bericht hat
pro Endpunkt eine Sektion mit Klassifikation und Detail-Diff.

CONSTRAINT: das Skript ueberschreibt KEIN Recording. Aenderungen am
Fixture-Bestand passieren nur ueber ``recording_session.py refresh``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable
from datetime import date as _date
from pathlib import Path
from typing import Any

# Repo-Root in sys.path, damit ``tests.cp_mock.diff`` erreichbar ist, wenn
# das Skript ueber ``python scripts/check_mock_drift.py`` aufgerufen wird
# (tests ist absichtlich kein installiertes Paket).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402

from broker_gateway import __version__ as bg_version  # noqa: E402
from broker_gateway.cp.normalize import normalize_response  # noqa: E402
from tests.cp_mock.diff import DiffReport, diff_recording  # noqa: E402


_DEFAULT_BASE_URL = "http://localhost:5000/v1/api"
_DEFAULT_FIXTURES_DIR = Path("tests/fixtures/recorded/live")
_DEFAULT_REPORT_DIR = Path("reports/drift")
_REQUEST_TIMEOUT_S = 30.0
_PRECONDITION_TIMEOUT_S = 10.0
# Warmup im Build-Acceptance-Modus: IBKR braucht nach Container-Start ~90s,
# bis Marktdaten/Portfolio-Endpunkte stabil antworten. Quelle: Auto-Memory
# project_ibkr_session_resume.
_BUILD_ACCEPTANCE_WARMUP_S = 90

# Pfad-Substrings, deren Recordings nicht erneut aufgerufen werden:
# Order-Endpunkte (Side Effects oder dokumentarisch, nicht drift-relevant)
# und Session-Wechsler (logout/reauthenticate beenden die Session).
_SKIP_PATH_SUBSTRINGS: tuple[str, ...] = (
    "/orders/whatif",
    "/orders/",
    "/order/",
    "/orders",
    "/logout",
    "/reauthenticate",
)


class DriftCheckError(RuntimeError):
    """Voraussetzungs- oder I/O-Fehler im Drift-Check."""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="check_mock_drift",
        description=(
            "Vergleicht Live-CP-Gateway-Antworten gegen die unter "
            "tests/fixtures/recorded/live/ eingecheckten Fixtures und "
            "schreibt einen Drift-Bericht nach reports/drift/."
        ),
    )
    p.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help=f"CP-Gateway Base-URL (default: {_DEFAULT_BASE_URL}).",
    )
    p.add_argument(
        "--fixtures-dir",
        type=Path,
        default=_DEFAULT_FIXTURES_DIR,
        help=f"Quellverzeichnis der Recordings (default: {_DEFAULT_FIXTURES_DIR}).",
    )
    p.add_argument(
        "--report-dir",
        type=Path,
        default=_DEFAULT_REPORT_DIR,
        help=f"Zielverzeichnis fuer Drift-Berichte (default: {_DEFAULT_REPORT_DIR}).",
    )
    p.add_argument(
        "--date",
        default=None,
        help="ISO-Datum fuer Berichtsdatei (default: heute).",
    )
    p.add_argument(
        "--build-acceptance",
        action="store_true",
        help=(
            "Strenger Build-Acceptance-Modus (AP-03): 90s Warmup vor "
            "dem ersten Replay, Exit 1 auch bei value drift, "
            "Bericht reports/drift/build-<sha>.md statt Datums-Datei. "
            "Voraussetzung: warmer Browser-Login (Skript versucht KEINEN "
            "automatischen Login)."
        ),
    )
    p.add_argument(
        "--commit-sha",
        default=None,
        help=(
            "Commit-SHA fuer den Build-Acceptance-Bericht (default: "
            "Env GIT_COMMIT bzw. CI_COMMIT_SHA, sonst 'unknown')."
        ),
    )
    p.add_argument(
        "--warmup-seconds",
        type=int,
        default=None,
        help=(
            "Warmup-Wartezeit (Sekunden) vor dem ersten Replay. "
            f"Default: 0 (manueller Modus), {_BUILD_ACCEPTANCE_WARMUP_S} "
            "im Build-Acceptance-Modus. 0 deaktiviert."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run_cli(args))


async def _run_cli(args: argparse.Namespace) -> int:
    fixtures = _list_fixtures(args.fixtures_dir)
    if not fixtures:
        print(
            f"VORAUSSETZUNG FEHLT: keine Recordings unter {args.fixtures_dir}.",
            file=sys.stderr,
        )
        return 2

    today = _date.fromisoformat(args.date) if args.date else _date.today()
    warmup = _resolve_warmup(args)
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=_REQUEST_TIMEOUT_S
    ) as client:
        try:
            await _ensure_authenticated(client)
        except DriftCheckError as exc:
            print(f"VORAUSSETZUNG FEHLT: {exc}", file=sys.stderr)
            if args.build_acceptance:
                # Im Build-Acceptance-Modus deutlicher: Browser-Login fehlt
                # heisst der Build muss explizit fehlschlagen.
                print(
                    "BUILD ABGEBROCHEN: Browser-Login + Reauth vorab durchfuehren "
                    "(siehe docs/runbooks/cpgateway-login.md).",
                    file=sys.stderr,
                )
            return 3

        if warmup > 0:
            print(f"[wait] Warmup {warmup}s ...")
            await asyncio.sleep(warmup)

        results = await run_drift_check(client, fixtures)

    report_path = _write_report(
        args.report_dir, today, results,
        build_acceptance=args.build_acceptance,
        commit_sha=_resolve_commit_sha(args) if args.build_acceptance else None,
    )
    print(f"[done] Drift-Bericht: {report_path}")

    summary = _summarize(results)
    print(
        "[done] no={no} minor={minor} value={value} breaking={breaking} skipped={skipped}"
        .format(**summary)
    )
    if args.build_acceptance:
        # Strenger: jeder breaking ODER value drift bricht den Build ab.
        # Minor (additive) bleibt tolerierbar - der Server hat ein neues Feld,
        # was Konsumenten nicht stoert. Timestamps werden ueber DEFAULT_IGNORE_FIELDS
        # bereits aus dem Diff entfernt - "value drift" hier heisst echte
        # Wertaenderung in einem nicht-ignorierten Feld.
        if summary["breaking"] > 0 or summary["value"] > 0:
            return 1
        return 0
    return 1 if summary["breaking"] > 0 else 0


def _resolve_warmup(args: argparse.Namespace) -> int:
    if args.warmup_seconds is not None:
        return max(0, args.warmup_seconds)
    if args.build_acceptance:
        return _BUILD_ACCEPTANCE_WARMUP_S
    return 0


def _resolve_commit_sha(args: argparse.Namespace) -> str:
    if args.commit_sha:
        return args.commit_sha
    return (
        os.environ.get("GIT_COMMIT")
        or os.environ.get("CI_COMMIT_SHA")
        or "unknown"
    )


# ---- Drift-Lauf ----


async def run_drift_check(
    client: httpx.AsyncClient,
    fixtures: Iterable[Path],
) -> list["DriftResult"]:
    """Fuehrt den Drift-Check fuer alle ``fixtures`` durch."""
    results: list[DriftResult] = []
    for fixture in fixtures:
        envelope = _load_envelope(fixture)
        request = envelope.get("request", {})
        response_recorded = envelope.get("response", {})
        path = request.get("url", "")
        method = (request.get("method") or "GET").upper()
        query = request.get("query") or {}
        body = request.get("body_json")

        if _should_skip(path, method):
            results.append(DriftResult(
                fixture=fixture,
                path=path, method=method,
                report=None, skip_reason="order/session-endpoint",
            ))
            continue

        # Live-Recording mit 4xx/5xx markiert dokumentarische Beweise -
        # diese sind nicht drift-fest und werden uebersprungen.
        if int(response_recorded.get("status_code") or 0) >= 400:
            results.append(DriftResult(
                fixture=fixture,
                path=path, method=method,
                report=None, skip_reason="recorded as 4xx/5xx",
            ))
            continue

        try:
            actual_resp = await _replay(client, method, path, query, body)
        except httpx.HTTPError as exc:
            results.append(DriftResult(
                fixture=fixture,
                path=path, method=method,
                report=None, skip_reason=f"transport-error: {exc}",
            ))
            continue

        if actual_resp.status_code != response_recorded.get("status_code"):
            # Status-Code-Aenderung ist immer breaking - synthesisch in
            # einen DiffReport mit changed_types verpacken.
            forced = DiffReport()
            forced.changed_types.append(
                _status_code_change(
                    expected=response_recorded.get("status_code"),
                    actual=actual_resp.status_code,
                )
            )
            results.append(DriftResult(
                fixture=fixture, path=path, method=method,
                report=forced, skip_reason=None,
            ))
            continue

        actual_body = _parse_body(actual_resp)
        actual_normalized = (
            normalize_response(actual_body, path)
            if actual_body is not None else None
        )
        expected_body = response_recorded.get("body_json")

        report = diff_recording(actual_normalized, expected_body)
        results.append(DriftResult(
            fixture=fixture, path=path, method=method,
            report=report, skip_reason=None,
        ))
    return results


def _should_skip(path: str, method: str) -> bool:
    for sub in _SKIP_PATH_SUBSTRINGS:
        if sub in path:
            return True
    if method in {"POST", "DELETE", "PUT", "PATCH"}:
        # Per Default keine schreibenden Calls im Drift-Check. /tickle ist
        # POST aber serverseitig idempotent - wird ueber Whitelist erlaubt.
        if path != "/tickle":
            return True
    return False


async def _replay(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    query: dict[str, str],
    body_json: Any,
) -> httpx.Response:
    if method == "GET":
        return await client.get(path, params=query)
    if method == "POST":
        return await client.post(path, params=query, json=body_json)
    raise DriftCheckError(f"Methode {method} nicht erlaubt im Drift-Check")


def _parse_body(resp: httpx.Response) -> Any:
    ct = resp.headers.get("content-type", "").lower()
    if "json" not in ct and not resp.content[:1] in (b"{", b"["):
        return None
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return None


def _status_code_change(*, expected: Any, actual: Any):
    from tests.cp_mock.diff import FieldChange
    return FieldChange(
        path="<http_status>", old=expected, new=actual,
        note=f"status {expected} -> {actual}",
    )


# ---- Fixture-Discovery ----


def _list_fixtures(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        f for f in directory.iterdir()
        if f.is_file()
        and f.suffix == ".json"
        and f.name != "live-recording-manifest.json"
    )


def _load_envelope(fixture: Path) -> dict[str, Any]:
    return json.loads(fixture.read_text(encoding="utf-8"))


# ---- Auth-Check ----


async def _ensure_authenticated(client: httpx.AsyncClient) -> None:
    try:
        resp = await client.get(
            "/iserver/auth/status", timeout=_PRECONDITION_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        raise DriftCheckError(
            f"CP-Gateway nicht erreichbar unter {client.base_url}: {exc}. "
            "Tunnel + cpgateway-Container pruefen."
        ) from exc

    if resp.status_code == 401:
        raise DriftCheckError(
            "/iserver/auth/status -> 401. "
            "Browser-Login fehlt - docs/runbooks/cpgateway-login.md vorher durchlaufen."
        )
    if resp.status_code != 200:
        raise DriftCheckError(
            f"Unerwarteter Status {resp.status_code} auf /iserver/auth/status."
        )

    body = resp.json()
    if not body.get("authenticated"):
        raise DriftCheckError(
            "auth/status meldet authenticated=false. "
            "Browser-Login durchfuehren - docs/runbooks/cpgateway-login.md."
        )


# ---- Bericht-Erzeugung ----


class DriftResult:
    __slots__ = ("fixture", "path", "method", "report", "skip_reason")

    def __init__(
        self,
        *,
        fixture: Path,
        path: str,
        method: str,
        report: DiffReport | None,
        skip_reason: str | None,
    ) -> None:
        self.fixture = fixture
        self.path = path
        self.method = method
        self.report = report
        self.skip_reason = skip_reason


def _summarize(results: list[DriftResult]) -> dict[str, int]:
    counts = {"no": 0, "minor": 0, "value": 0, "breaking": 0, "skipped": 0}
    for r in results:
        if r.report is None:
            counts["skipped"] += 1
            continue
        cls = r.report.classification
        if cls == "no drift":
            counts["no"] += 1
        elif cls == "minor drift (additive)":
            counts["minor"] += 1
        elif cls == "value drift":
            counts["value"] += 1
        elif cls == "breaking drift":
            counts["breaking"] += 1
    return counts


def _write_report(
    report_dir: Path,
    today: _date,
    results: list[DriftResult],
    *,
    build_acceptance: bool = False,
    commit_sha: str | None = None,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    if build_acceptance:
        sha = commit_sha or "unknown"
        # Kurzform fuer lange SHAs (Lesbarkeit + Datei-Namens-Limit auf
        # einigen Filesystemen). Volle Form bleibt im Bericht-Body.
        short_sha = sha[:12] if len(sha) > 12 else sha
        target = report_dir / f"build-{short_sha}.md"
    else:
        target = report_dir / f"{today.isoformat()}.md"
    summary = _summarize(results)

    lines: list[str] = []
    lines.append(f"# Drift-Bericht {today.isoformat()}")
    lines.append("")
    lines.append(f"broker-gateway {bg_version}")
    lines.append("")
    lines.append("## Zusammenfassung")
    lines.append("")
    lines.append(
        f"- no drift: {summary['no']}"
        f" - minor drift (additive): {summary['minor']}"
        f" - value drift: {summary['value']}"
        f" - **breaking drift: {summary['breaking']}**"
        f" - uebersprungen: {summary['skipped']}"
    )
    if summary["breaking"]:
        lines.append("")
        lines.append("> ESKALATION: mindestens ein Endpunkt zeigt breaking drift.")
        lines.append("> Konsumenten (PSM, trading-robot) koennen betroffen sein.")
        lines.append("> Karte mit blocked=true anlegen, bevor neue Fixture eingespielt wird.")
    lines.append("")
    lines.append("## Endpunkte")
    lines.append("")

    for result in results:
        title = f"{result.method} {result.path}"
        if result.report is None:
            lines.append(f"### {title}")
            lines.append("")
            lines.append(f"- uebersprungen: {result.skip_reason}")
            lines.append("")
            continue
        lines.append(result.report.render_markdown(title=title))

    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


if __name__ == "__main__":
    sys.exit(main())
