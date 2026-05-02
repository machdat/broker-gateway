#!/usr/bin/env python3
"""Pre-Commit-Hook: scannt staged Recording-JSON/JSONL auf Token-Leaks.

Entscheidet pro Datei, ob sie sensible Inhalte enthaelt, die der Recorder
nicht haette durchlassen duerfen, oder die ein Mensch versehentlich
eincheckt. Aktivierung:

    pre-commit install        # einmal pro Clone
    pre-commit run --all-files  # ad-hoc-Lauf

Dieser Hook wird von ``.pre-commit-config.yaml`` als ``files``-gefilterter
local-hook angeworfen - der Aufrufer (pre-commit) reicht die staged
Recording-Dateien als positionale Argumente. Wenn das Skript direkt ohne
Argumente gestartet wird, wird ohne Fund Exit 0 zurueckgegeben (no-op).

Drei Heuristiken:

* **Header-Namen**: Keys aus :data:`broker_gateway.cp.redaction.REDACTED_HEADERS`
  duerfen in Recording-Dateien nirgends auftauchen - der Recorder filtert
  sie bereits beim Schreiben. Wenn sie hier auftauchen, ist die Datei
  manuell editiert worden oder der Recorder-Filter wurde umgangen.
* **URL-safe-Token**: Strings >= 32 Zeichen aus ``[A-Za-z0-9_-]`` sind
  charakteristisch fuer Bearer-Token, JWT und API-Keys. Falsche
  Positives bei SHA256-Hashes werden ueber eine Allowlist von bekannten
  Hash-Feldern (``MAC``, ``hardware_info``, ``etag``,
  ``server-timing``, ``x-request-id``, ``request_id``) abgefangen.
* **Cookie-Pattern**: typische Session-Cookies wie ``sess=``,
  ``X-XSRF-TOKEN=``, ``_csrf=``, ``JSESSIONID=`` als Substring im Body.

Bei Fund: Exit-Code != 0; pro Verletzung eine Zeile mit Datei, Pfad im
JSON-Baum und gekuerztem Wert.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Single Source of Truth fuer die zu schwaerzenden Header. Import erst
# nach dem sys.path-Eingriff, damit das Skript ohne ``pip install -e .``
# laeuft.
from broker_gateway.cp.redaction import REDACTED_HEADERS  # noqa: E402


# Felder, in denen URL-safe-Strings >= 32 Zeichen erlaubt sind, weil sie
# nachweislich Hash-/Server-IDs sind (vom Recorder schon redacted oder
# von IBKR als undurchsichtige Identifier geliefert) - oder weil sie
# strukturierte Identifier sind, die zwar URL-safe und lang sind aber
# nichts auslecken (Recording-Filenames, IBKR-Warning-Codes).
ALLOWLIST_HASH_FIELDS: frozenset[str] = frozenset({
    # Hash-/Server-Felder vom IBKR-Stack
    "mac",
    "hardware_info",
    "etag",
    "server-timing",
    "x-request-id",
    "request_id",
    "user-agent",
    # Strukturierte Identifier in Manifest-/Antwort-Bodies
    "files",            # live-recording-manifest.json: Liste der Recording-Filenames
    "warns",            # whatif-Response: IBKR-Warning-Codes wie 'market_order_confirmation_warning'
    "warning_code",     # whatif-Warnings pro Eintrag
    "warning_message",  # whatif-Warnings pro Eintrag
})

# Token-Heuristik: URL-safe-String aus [A-Za-z0-9_-] mit Mindestlaenge.
_MIN_TOKEN_LEN = 32
_URL_SAFE_TOKEN = re.compile(rf"[A-Za-z0-9_\-]{{{_MIN_TOKEN_LEN},}}")

_COOKIE_PATTERNS = (
    "sess=",
    "X-XSRF-TOKEN=",
    "_csrf=",
    "JSESSIONID=",
)

# Strings, die der Recorder explizit als Platzhalter setzt - werden vom
# Token-Scan ausgenommen, weil sie nichts auslecken.
_REDACTION_PLACEHOLDERS = ("<REDACTED>", "REDACTED")


class Violation:
    """Ein Treffer mit Datei + JSON-Pfad + gekuerztem Wert."""

    __slots__ = ("file", "kind", "path", "value")

    def __init__(self, file: Path, kind: str, path: str, value: str) -> None:
        self.file = file
        self.kind = kind
        self.path = path
        self.value = value

    def render(self) -> str:
        snippet = self.value if len(self.value) <= 60 else f"{self.value[:57]}..."
        return f"  - {self.kind} @ {self.path}: {snippet!r}"


def scan_value(
    value: Any,
    *,
    json_path: str,
    parent_key: str | None = None,
    check_urlsafe: bool = True,
) -> Iterator[tuple[str, str, str]]:
    """Rekursiver Scanner. Liefert ``(kind, path, raw_value)``-Tupel.

    ``parent_key`` ist der Dict-Schluessel, unter dem ``value`` haengt -
    er entscheidet ueber die Allowlist (Hash-Felder werden vom
    URL-safe-Pattern befreit). ``check_urlsafe`` schaltet die
    Token-Heuristik ab; in WS-Recordings ist das sinnvoll, weil
    Frame-IDs und Session-IDs strukturell URL-safe-32-stellig sind und
    den Scanner mit False-Positives ueberfluten.
    """
    if isinstance(value, dict):
        for key, sub in value.items():
            sub_path = f"{json_path}.{key}" if json_path else key
            if key.lower() in REDACTED_HEADERS:
                yield ("redacted_header", sub_path, key)
            yield from scan_value(
                sub,
                json_path=sub_path,
                parent_key=key,
                check_urlsafe=check_urlsafe,
            )
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from scan_value(
                item,
                json_path=f"{json_path}[{i}]",
                parent_key=parent_key,
                check_urlsafe=check_urlsafe,
            )
    elif isinstance(value, str):
        # Cookie-Patterns suchen (case-sensitive bewusst - Cookies sind
        # konventionell case-stable, und einige Patterns wie 'sess=' sind
        # ohne case-folding eindeutig).
        for cookie_pat in _COOKIE_PATTERNS:
            if cookie_pat in value:
                yield ("cookie_pattern", json_path, cookie_pat)
        # Token-Heuristik: URL-safe-String, nicht in Allowlist, nicht ein
        # Platzhalter. In WS-Recordings deaktiviert.
        if not check_urlsafe:
            return
        parent_lower = (parent_key or "").lower()
        if parent_lower not in ALLOWLIST_HASH_FIELDS:
            for match in _URL_SAFE_TOKEN.finditer(value):
                token = match.group(0)
                if any(token == ph for ph in _REDACTION_PLACEHOLDERS):
                    continue
                yield ("urlsafe_token", json_path, token)


def is_ws_recording(path: Path) -> bool:
    """True, wenn ``path`` unter tests/fixtures/recorded/ws/ liegt."""
    parts = [p.lower() for p in path.parts]
    return (
        "tests" in parts
        and "fixtures" in parts
        and "recorded" in parts
        and "ws" in parts
    )


def scan_file(path: Path) -> list[Violation]:
    """Liest eine JSON- oder JSONL-Datei und liefert die Verletzungen."""
    violations: list[Violation] = []
    suffix = path.suffix.lower()
    text = path.read_text("utf-8")
    if suffix == ".jsonl":
        documents = [
            (i, json.loads(line))
            for i, line in enumerate(text.splitlines())
            if line.strip()
        ]
    else:
        documents = [(0, json.loads(text))]

    check_urlsafe = not is_ws_recording(path)
    for line_idx, document in documents:
        prefix = f"line[{line_idx}]" if suffix == ".jsonl" else ""
        for kind, json_path, raw_value in scan_value(
            document, json_path=prefix, check_urlsafe=check_urlsafe
        ):
            violations.append(Violation(path, kind, json_path, raw_value))
    return violations


def is_recording_path(path: Path) -> bool:
    """True, wenn ``path`` unter tests/fixtures/recorded/ liegt."""
    parts = [p.lower() for p in path.parts]
    if "tests" not in parts or "fixtures" not in parts or "recorded" not in parts:
        return False
    suffix = path.suffix.lower()
    return suffix in (".json", ".jsonl")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scannt Recording-JSON/JSONL auf Token-Leaks."
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="staged Dateipfade (von pre-commit gereicht). Nicht-Recording-Pfade werden ignoriert.",
    )
    args = parser.parse_args(argv)

    targets = [p for p in args.files if is_recording_path(p)]
    if not targets:
        return 0

    all_violations: list[Violation] = []
    for path in targets:
        if not path.exists():
            # pre-commit liefert nur staged Pfade; eine fehlende Datei
            # ist ein Setup-Fehler des Aufrufers, kein Scan-Treffer.
            print(f"warn: {path} existiert nicht, uebersprungen.", file=sys.stderr)
            continue
        try:
            file_violations = scan_file(path)
        except json.JSONDecodeError as exc:
            print(f"error: {path}: kein gueltiges JSON ({exc})", file=sys.stderr)
            return 2
        if file_violations:
            print(f"FAIL {path}", file=sys.stderr)
            for v in file_violations:
                print(v.render(), file=sys.stderr)
            all_violations.extend(file_violations)

    if all_violations:
        print(
            f"\n{len(all_violations)} Token-Leak-Verdacht in {len(targets)} Recording-Dateien.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
