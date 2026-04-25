#!/usr/bin/env python3
"""Skelett fuer Live-Recording-Sessions gegen das CP-Gateway.

In dieser Karte (AP-02 #02) entsteht NUR der Argument-Parser und das
Sichtbarmachen der Konfiguration. Die eigentlichen Live-Aufrufe gegen
U25235077 werden in den Folgekarten umgesetzt:

- AP-02 #04: Happy-Path - alle v1-Endpunkte einmal aufzeichnen.
- AP-02 #05: Error-Path - 401 / 503 / 429 / Reply-Confirmation-Loop /
  pacing-violation explizit erzwingen und aufzeichnen.

Bewusst kein httpx-Call hier: die Folgekarten sollen die Endpunkt-Liste
und das Reauth-Handling sauber abhandeln, statt jetzt einen halben
Recorder-Lauf einzucheckend, der bei der naechsten Erweiterung wieder
umgeschrieben werden muss.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recording_session",
        description=(
            "Live-Recording-Session gegen das interne IBKR Client Portal "
            "Gateway. Skelett - eigentliche Endpunkt-Aufrufe folgen in "
            "AP-02 #04 (happy) und AP-02 #05 (error)."
        ),
    )
    p.add_argument(
        "--record-dir",
        type=Path,
        required=True,
        help="Zielverzeichnis fuer die JSON-Fixtures (z.B. tests/fixtures/recorded).",
    )
    p.add_argument(
        "--base-url",
        default="http://cpgateway:5000/v1/api",
        help="Base-URL des CP-Gateways. Default passt zum Compose-Stack.",
    )
    p.add_argument(
        "--account-id",
        default="U25235077",
        help="IBKR-Konto-ID, gegen die aufgezeichnet wird.",
    )
    p.add_argument(
        "--scenario",
        choices=("happy", "error"),
        default="happy",
        help="happy = AP-02 #04 (alle Endpunkte einmal), error = AP-02 #05.",
    )
    p.add_argument(
        "--normalize-prices",
        action="store_true",
        help=(
            "Wenn gesetzt, werden auch Preis- und Marktdaten-Felder durch "
            "Platzhalter ersetzt. Default: aus, weil Preise die Realitaet "
            "darstellen, gegen die Tests laufen sollen."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    print("Recording-Session-Konfiguration:")
    print(f"  record_dir        = {args.record_dir.resolve()}")
    print(f"  base_url          = {args.base_url}")
    print(f"  account_id        = {args.account_id}")
    print(f"  scenario          = {args.scenario}")
    print(f"  normalize_prices  = {args.normalize_prices}")
    print()
    print(
        "Hinweis: Live-Endpunkt-Aufrufe sind noch nicht implementiert. "
        "Der echte Aufzeichnungslauf folgt in AP-02 Karte #04 (happy-path) "
        "bzw. #05 (error-path). Recorder-Mechanik (header-filter, "
        "first-call-prime, normalize_response) ist bereits einsatzbereit."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
