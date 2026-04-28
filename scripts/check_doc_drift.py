#!/usr/bin/env python3
"""Doku-Drift-Check: IBKR-OpenAPI-Spec gegen die eingecheckte Baseline (AP-03).

Frueh-Warner-Mechanismus, der **ohne** Live-Auth funktioniert: laedt die
aktuelle IBKR-Spec via HTTP und vergleicht sie via
:func:`tests.cp_doc.diff.diff_openapi` gegen
``docs/research/ibkr-cpapi-doc.json``. Schreibt einen Markdown-Bericht
nach ``reports/doc-drift/<YYYY-MM-DD>.md``.

Exit-Codes:

- ``0`` - kein Drift (oder nur ``value``-Drift).
- ``1`` - **breaking drift** entdeckt - sofort eskalieren.
- ``2`` - **minor drift** entdeckt (additive). Cron muss "Failure" zeigen
  damit der Mensch reagiert (Karte anlegen, Spec-Update committen).
- ``3`` - Quell-URL nicht erreichbar / I/O-Fehler.

Optional ``--auto-card``: bei Drift mit Exit 1 oder 2 wird via KanPrompt-
REST-API eine Karte angelegt. Spam-Schutz: maximal 1 Karte pro Tag pro
Drift-Klasse, gepueft via ``GET /api/v1/projects/{pid}/cards`` mit Filter
auf das heute generierte Title-Praefix.

CONSTRAINT: das Skript ueberschreibt **niemals** die eingecheckte
Baseline ``docs/research/ibkr-cpapi-doc.json``. Aenderungen an der
Baseline sind eine bewusste menschliche Entscheidung.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Any

# Repo-Root + src/ in sys.path, damit ``tests.cp_doc.diff`` und
# ``broker_gateway`` ohne ``pip install -e .`` erreichbar sind. Das
# systemd-Setup auf cma-pi-1 nutzt ein schlankes venv mit nur httpx.
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import httpx  # noqa: E402

from broker_gateway import __version__ as bg_version  # noqa: E402
from tests.cp_doc.diff import SpecDiffReport, diff_openapi  # noqa: E402


_DEFAULT_SOURCE_URL = "https://www.interactivebrokers.com/api/doc.json"
_DEFAULT_BASELINE = Path("docs/research/ibkr-cpapi-doc.json")
_DEFAULT_REPORT_DIR = Path("reports/doc-drift")
_HTTP_TIMEOUT_S = 30.0
_KANPROMPT_DEFAULT_BASE = "http://cma-pi-1:8000"
_BROKER_GATEWAY_PROJECT_ID = "a6a45428-ac37-48f5-b295-d3ff26f31711"

# Exit-Codes nach AP-03 Karte
EXIT_OK = 0
EXIT_BREAKING = 1
EXIT_MINOR = 2
EXIT_UNREACHABLE = 3

# Mapping Drift-Klasse -> Exit-Code.
_CLASSIFICATION_EXITCODE: dict[str, int] = {
    "no drift": EXIT_OK,
    "value (irrelevant)": EXIT_OK,
    "minor (additive)": EXIT_MINOR,
    "breaking": EXIT_BREAKING,
}


class DocDriftError(RuntimeError):
    """Voraussetzungs- oder I/O-Fehler im Doku-Drift-Check."""


@dataclass
class CheckOutcome:
    """Ergebnis eines Lauf-Aufrufs - fuer Tests einfacher zu verifizieren als nur Exit-Code."""
    exit_code: int
    classification: str
    report_path: Path | None
    card_created: bool


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="check_doc_drift",
        description=(
            "Vergleicht die Live-IBKR-OpenAPI-Spec gegen die eingecheckte "
            "Baseline und schreibt einen Drift-Bericht. Bei --auto-card "
            "wird bei Drift eine KanPrompt-Karte angelegt."
        ),
    )
    p.add_argument(
        "--source-url",
        default=_DEFAULT_SOURCE_URL,
        help=f"Quell-URL der Live-Spec (default: {_DEFAULT_SOURCE_URL}).",
    )
    p.add_argument(
        "--baseline",
        type=Path,
        default=_DEFAULT_BASELINE,
        help=f"Pfad der eingecheckten Baseline (default: {_DEFAULT_BASELINE}).",
    )
    p.add_argument(
        "--report-dir",
        type=Path,
        default=_DEFAULT_REPORT_DIR,
        help=f"Zielverzeichnis fuer Berichte (default: {_DEFAULT_REPORT_DIR}).",
    )
    p.add_argument(
        "--date",
        default=None,
        help="ISO-Datum fuer Berichtsdatei (default: heute).",
    )
    p.add_argument(
        "--auto-card",
        action="store_true",
        help=(
            "Bei Drift (exit 1/2) eine KanPrompt-Karte anlegen. "
            "Erfordert KANPROMPT_API_KEY in der Umgebung."
        ),
    )
    p.add_argument(
        "--kanprompt-base-url",
        default=os.environ.get("KANPROMPT_BASE_URL", _KANPROMPT_DEFAULT_BASE),
        help=(
            "KanPrompt-API Basis-URL (default Env KANPROMPT_BASE_URL bzw. "
            f"{_KANPROMPT_DEFAULT_BASE})."
        ),
    )
    p.add_argument(
        "--project-id",
        default=_BROKER_GATEWAY_PROJECT_ID,
        help="KanPrompt-Projekt-ID fuer Auto-Karten (default broker-gateway).",
    )
    p.add_argument(
        "--http-client",
        default=None,
        help=argparse.SUPPRESS,  # interner Hook fuer Tests
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        outcome = run(
            source_url=args.source_url,
            baseline_path=args.baseline,
            report_dir=args.report_dir,
            today=_date.fromisoformat(args.date) if args.date else _date.today(),
            auto_card=args.auto_card,
            kanprompt_base_url=args.kanprompt_base_url,
            project_id=args.project_id,
        )
    except DocDriftError as exc:
        print(f"VORAUSSETZUNG FEHLT: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    return outcome.exit_code


def run(
    *,
    source_url: str,
    baseline_path: Path,
    report_dir: Path,
    today: _date,
    auto_card: bool,
    kanprompt_base_url: str,
    project_id: str,
    http_client_factory: Any = None,
) -> CheckOutcome:
    """Fuehrt den Doku-Drift-Check aus.

    ``http_client_factory`` ist ein optionaler Hook fuer Tests: muss eine
    Funktion ``() -> httpx.Client`` zurueckliefern. Default: echter
    httpx.Client gegen die echte Quell-URL.
    """
    expected_spec = _load_baseline(baseline_path)

    client_factory = http_client_factory or _default_client_factory
    try:
        actual_spec = _fetch_spec(source_url, client_factory)
    except DocDriftError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocDriftError(
            f"Live-Spec von {source_url} nicht ladbar: {exc}"
        ) from exc

    report = diff_openapi(actual_spec, expected_spec)
    report_path = _write_report(
        report_dir=report_dir,
        today=today,
        report=report,
        source_url=source_url,
        baseline_path=baseline_path,
    )
    classification = report.classification
    exit_code = _CLASSIFICATION_EXITCODE.get(classification, EXIT_OK)

    print(f"[done] Doku-Drift-Bericht: {report_path}")
    print(f"[done] Klassifikation: {classification}")

    card_created = False
    if auto_card and exit_code in (EXIT_BREAKING, EXIT_MINOR):
        try:
            card_created = _maybe_create_card(
                kanprompt_base_url=kanprompt_base_url,
                project_id=project_id,
                today=today,
                report=report,
                report_path=report_path,
                classification=classification,
                client_factory=client_factory,
            )
        except DocDriftError as exc:
            print(f"WARNUNG: Auto-Karten-Anlage fehlgeschlagen: {exc}", file=sys.stderr)

    return CheckOutcome(
        exit_code=exit_code,
        classification=classification,
        report_path=report_path,
        card_created=card_created,
    )


# ---- Spec-Laden ---------------------------------------------------------


def _default_client_factory() -> httpx.Client:
    return httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True)


def _fetch_spec(source_url: str, client_factory: Any) -> dict[str, Any]:
    headers = {
        # IBKR liefert ohne UA gerne 403/HTML zurueck.
        "User-Agent": f"broker-gateway/{bg_version} doc-drift",
        "Accept": "application/json",
    }
    with client_factory() as client:
        try:
            resp = client.get(source_url, headers=headers)
        except httpx.HTTPError as exc:
            raise DocDriftError(
                f"HTTP-Fehler beim Laden der Spec ({source_url}): {exc}"
            ) from exc
    if resp.status_code != 200:
        raise DocDriftError(
            f"Spec-Endpunkt {source_url} antwortete mit Status {resp.status_code}."
        )
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise DocDriftError(
            f"Spec-Antwort von {source_url} ist kein gueltiges JSON: {exc}"
        ) from exc


def _load_baseline(baseline_path: Path) -> dict[str, Any]:
    if not baseline_path.is_file():
        raise DocDriftError(
            f"Baseline-Datei {baseline_path} nicht gefunden."
        )
    try:
        return json.loads(baseline_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise DocDriftError(
            f"Baseline {baseline_path} ist kein gueltiges JSON: {exc}"
        ) from exc


# ---- Bericht ------------------------------------------------------------


def _write_report(
    *,
    report_dir: Path,
    today: _date,
    report: SpecDiffReport,
    source_url: str,
    baseline_path: Path,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    target = report_dir / f"{today.isoformat()}.md"

    lines: list[str] = []
    lines.append(f"# Doku-Drift-Bericht {today.isoformat()}")
    lines.append("")
    lines.append(f"broker-gateway {bg_version}")
    lines.append("")
    lines.append(f"- Quelle: {source_url}")
    lines.append(f"- Baseline: `{baseline_path}`")
    lines.append("")
    lines.append("## Zusammenfassung")
    lines.append("")
    lines.append(
        f"- breaking: {len(report.breaking_findings())}"
        f" - minor (additive): {len(report.minor_findings())}"
        f" - value: {len(report.value_findings())}"
    )
    lines.append("")
    lines.append(f"**Klassifikation:** {report.classification}")
    lines.append("")
    if report.is_clean():
        lines.append("Keine Drift festgestellt.")
        target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return target

    if report.breaking_findings():
        lines.append("## Breaking")
        for f in report.breaking_findings():
            lines.append(_render_finding(f))
        lines.append("")
    if report.minor_findings():
        lines.append("## Minor (additive)")
        for f in report.minor_findings():
            lines.append(_render_finding(f))
        lines.append("")
    if report.value_findings():
        lines.append("## Value (irrelevant)")
        for f in report.value_findings():
            lines.append(_render_finding(f))
        lines.append("")

    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


def _render_finding(finding: Any) -> str:
    detail = f": {finding.detail}" if finding.detail else ""
    return f"- `{finding.path}` ({finding.kind}){detail}"


# ---- Auto-Karten-Anlage -------------------------------------------------


_CARD_TITLE_PREFIX = "Doku-Drift"


def _maybe_create_card(
    *,
    kanprompt_base_url: str,
    project_id: str,
    today: _date,
    report: SpecDiffReport,
    report_path: Path,
    classification: str,
    client_factory: Any,
) -> bool:
    api_key = os.environ.get("KANPROMPT_API_KEY")
    if not api_key:
        raise DocDriftError(
            "KANPROMPT_API_KEY ist nicht gesetzt - Auto-Karten-Anlage nicht moeglich."
        )

    title_prefix = _title_prefix(classification, today)
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    base_url = kanprompt_base_url.rstrip("/")

    with client_factory() as client:
        # Spam-Schutz: bereits heute eine Karte mit dem gleichen Praefix?
        if _has_card_with_prefix(client, base_url, project_id, title_prefix, headers):
            print(
                f"[skip] Heute existiert bereits eine Karte mit Praefix '{title_prefix}'. "
                "Karten-Spam vermieden.",
                file=sys.stderr,
            )
            return False

        body = _build_card_body(
            classification=classification,
            today=today,
            report=report,
            report_path=report_path,
            title_prefix=title_prefix,
        )
        try:
            resp = client.post(
                f"{base_url}/api/v1/projects/{project_id}/cards",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise DocDriftError(f"KanPrompt POST fehlgeschlagen: {exc}") from exc
    if resp.status_code not in (200, 201):
        raise DocDriftError(
            f"KanPrompt antwortete mit Status {resp.status_code}: {resp.text}"
        )

    print(f"[done] KanPrompt-Karte angelegt: {title_prefix}")
    return True


def _title_prefix(classification: str, today: _date) -> str:
    severity_token = (
        "breaking" if classification == "breaking"
        else "minor"
    )
    return f"{_CARD_TITLE_PREFIX} {severity_token} {today.isoformat()}"


def _has_card_with_prefix(
    client: httpx.Client,
    base_url: str,
    project_id: str,
    title_prefix: str,
    headers: dict[str, str],
) -> bool:
    """Prueft ob heute schon eine Karte mit gegebenem Praefix existiert."""
    try:
        resp = client.get(
            f"{base_url}/api/v1/projects/{project_id}/cards",
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise DocDriftError(
            f"KanPrompt GET fehlgeschlagen: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise DocDriftError(
            f"KanPrompt /cards antwortete mit Status {resp.status_code}."
        )
    try:
        cards = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise DocDriftError(f"KanPrompt-Antwort kein JSON: {exc}") from exc
    if not isinstance(cards, list):
        # Manche APIs verpacken in {"cards": [...]} oder {"items": [...]}.
        cards = cards.get("cards") or cards.get("items") or []
    for card in cards:
        if not isinstance(card, dict):
            continue
        title = str(card.get("title") or "")
        if title.startswith(title_prefix):
            return True
    return False


def _build_card_body(
    *,
    classification: str,
    today: _date,
    report: SpecDiffReport,
    report_path: Path,
    title_prefix: str,
) -> dict[str, Any]:
    is_breaking = classification == "breaking"

    summary_lines: list[str] = []
    if report.breaking_findings():
        summary_lines.append(f"- breaking: {len(report.breaking_findings())}")
    if report.minor_findings():
        summary_lines.append(f"- minor (additive): {len(report.minor_findings())}")
    if report.value_findings():
        summary_lines.append(f"- value: {len(report.value_findings())}")

    detail_lines: list[str] = []
    for f in report.breaking_findings()[:20]:
        detail_lines.append(_render_finding(f))
    if len(report.breaking_findings()) > 20:
        detail_lines.append(f"- ... und {len(report.breaking_findings()) - 20} weitere breaking-Findings.")

    return {
        "title": f"{title_prefix}: IBKR-Spec-Drift festgestellt",
        "card_type": "bugfix" if is_breaking else "feature",
        "priority": 0 if is_breaking else 1,
        "blocked": is_breaking,
        "autonomy_level": 1,
        "deployment_required": False,
        "problem": (
            f"Auto-erkannter Doku-Drift am {today.isoformat()}. "
            f"Klassifikation: {classification}. "
            f"Bericht: {report_path}."
        ),
        "ist_zustand": "\n".join(summary_lines) or "- keine Zaehlung",
        "soll_zustand": (
            "Spec-Aenderung pruefen und entscheiden:\n"
            "- minor: Baseline-JSON updaten und Schema-Doku ergaenzen.\n"
            "- breaking: Konsumenten (PSM, trading-robot) informieren, "
            "Folge-Karte mit blocked=true anlegen, Schema-Migrations-Plan "
            "erstellen.\n\n"
            "Detail-Findings:\n" + ("\n".join(detail_lines) or "- siehe Bericht-Datei.")
        ),
        "constraints": (
            "Baseline docs/research/ibkr-cpapi-doc.json darf nur nach "
            "menschlicher Pruefung aktualisiert werden. Auto-Skript "
            "schreibt nicht."
        ),
        "verification": (
            "- [ ] Bericht gelesen\n"
            "- [ ] Drift-Klasse bestaetigt\n"
            "- [ ] Folgeaktionen entschieden (Update vs. Eskalation)"
        ),
        "affected_files": "docs/research/ibkr-cpapi-doc.json",
    }


if __name__ == "__main__":
    sys.exit(main())
