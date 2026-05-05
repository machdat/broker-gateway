"""Browser-unabhaengige Logik des Auto-Login-Sidecars.

Diese Hilfen laufen ohne Playwright und sind direkt unit-testbar.
Der eigentliche Browser-Flow lebt in ``auto_login.py`` und ruft
diese Helfer.

Bewusst minimal: nur die Stuecke, die Klartext-Credentials anfassen
oder Sicherheits-Entscheidungen treffen. Der Rest (Browser-Steuerung)
wird durch Live-Smokes verifiziert, weil Playwright-Mocks die
realistische Form-Mechanik nicht replizieren.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass


# Exit-Codes nach Karten-Spec (auch in cp/auto_login_trigger.py
# referenziert; Konsistenz hier ist Pflicht).
EXIT_OK = 0
EXIT_FORM_NOT_FOUND = 1
EXIT_LOGIN_REFUSED = 2
EXIT_NETWORK = 3
EXIT_2FA = 4
EXIT_HARD_GUARD = 5
EXIT_OTHER = 9


_PAPER_HOST_TOKEN = "paper-cpgateway"


def mask_username(value: str) -> str:
    """`cborlm399` -> `cb***99`. Bei zu kurzen Werten generischer Mask."""
    if not value:
        return ""
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def is_paper_target(url: str) -> bool:
    """Hard-Guard: erlaubt nur URLs, die `paper-cpgateway` enthalten.

    Das ist die letzte Verteidigungslinie im Sidecar selbst — der
    Trigger sollte den Sidecar gar nicht erst aufrufen, wenn der
    Stack `live` ist. Aber falls die Trigger-Logik durch einen Bug
    umgangen wuerde, schliesst diese Pruefung den Live-cpgateway
    weiterhin aus.
    """
    return _PAPER_HOST_TOKEN in (url or "")


def classify_dispatcher(status: int, body: str) -> int:
    """Mapped die Dispatcher-Antwort auf einen Exit-Code.

    HTTP 200 + Body enthaelt 'Client login succeeds' -> EXIT_OK.
    HTTP 200 + anderer Body -> EXIT_LOGIN_REFUSED (z.B. abgewiesen,
        Captcha-HTML).
    HTTP != 200 -> EXIT_LOGIN_REFUSED (Server hat den Submit nicht
        akzeptiert).
    """
    if status != 200:
        return EXIT_LOGIN_REFUSED
    if "Client login succeeds" in (body or ""):
        return EXIT_OK
    return EXIT_LOGIN_REFUSED


@dataclass(frozen=True)
class JsonLogEvent:
    """Strukturiertes Log-Event fuer stdout."""

    phase: str
    fields: dict


def emit_log(event: JsonLogEvent, *, stream=None) -> str:
    """Schreibt das Event als JSON-Zeile auf ``stream`` (Default stdout).

    Gibt die geschriebene Zeile zurueck, damit Tests den Inhalt
    pruefen koennen ohne stdout abfangen zu muessen.

    Defensive: keine ``ensure_ascii=False`` — wir bleiben auf reinem
    ASCII fuer maximale Log-Aggregation-Kompatibilitaet (z.B. journald).
    """
    payload = {"ts": time.time(), "phase": event.phase, **event.fields}
    line = json.dumps(payload, sort_keys=True)
    target = stream if stream is not None else sys.stdout
    target.write(line + "\n")
    target.flush()
    return line


__all__ = [
    "EXIT_OK",
    "EXIT_FORM_NOT_FOUND",
    "EXIT_LOGIN_REFUSED",
    "EXIT_NETWORK",
    "EXIT_2FA",
    "EXIT_HARD_GUARD",
    "EXIT_OTHER",
    "JsonLogEvent",
    "classify_dispatcher",
    "emit_log",
    "is_paper_target",
    "mask_username",
]
