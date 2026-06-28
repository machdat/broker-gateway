"""Schmale ENV-Konfigurationsschicht.

Single Source of Truth fuer ENV-Variablen, die quer durch den Service
gelesen werden. Alle ``BG_``-praefixierten Werte mit Default-Fallback.

Bewusst nicht: pydantic-Settings, Plugin-Discovery, hierarchische
Config-Files. Der Service ist klein genug, dass ``os.environ.get`` mit
Wrapper-Funktion reicht.
"""
from __future__ import annotations

import logging
import os
from typing import Final, Literal


logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Konfigurations-Fehler beim Service-Startup.

    Bewusst eigener Typ, damit ``main.py`` ihn vor dem Lifespan-Start
    fangen und mit klarer Fehlermeldung in den Logs absetzen kann.
    """


# Wert von ``BG_QUOTES_SOURCE``: ``ws`` aktiviert den WSPushSource-Pfad,
# ``polling`` belaesst es bei dem klassischen REST-Polling.
QuotesSource = Literal["ws", "polling"]


_QUOTES_SOURCE_ENV: Final[str] = "BG_QUOTES_SOURCE"
_QUOTES_SOURCE_DEFAULT: Final[QuotesSource] = "polling"
_QUOTES_SOURCE_VALID: Final[frozenset[str]] = frozenset({"ws", "polling"})


def quotes_source() -> QuotesSource:
    """Liefert die Quelle fuer den ``/v1/quotes/stream``-SSE-Pfad.

    Default ist ``polling`` (Bestand), nicht ``ws`` wie das K6-Design
    vorsieht. Begruendung: der WS-Pfad braucht eine aktive
    ``CPWebSocketClient``-Connection und einen geloggten Account; vor
    dem 2FA-Login (Cold-Start) ist das nicht verfuegbar. Mit Default
    ``polling`` laeuft der Service ohne ENV-Pflege wie heute, ``ws``
    ist Opt-in. Sobald der WS-Pfad einen Trading-Tag stabil laeuft
    (Phase-A-Deploy-Bedingung gemaess Sektion 6.2 des K6-Designs),
    kann der Default in einer Folge-Karte auf ``ws`` springen.
    """
    raw = os.environ.get(_QUOTES_SOURCE_ENV, _QUOTES_SOURCE_DEFAULT).strip().lower()
    if raw not in _QUOTES_SOURCE_VALID:
        logger.warning(
            "ENV %s=%r ist keiner von %s, fallback auf %s",
            _QUOTES_SOURCE_ENV,
            raw,
            sorted(_QUOTES_SOURCE_VALID),
            _QUOTES_SOURCE_DEFAULT,
        )
        return _QUOTES_SOURCE_DEFAULT
    return raw  # type: ignore[return-value]


# ---- Stack-Kind (live | paper) ----

StackKind = Literal["live", "paper"]

_STACK_KIND_ENV: Final[str] = "BG_STACK_KIND"
_STACK_KIND_VALID: Final[frozenset[str]] = frozenset({"live", "paper"})


def stack_kind() -> StackKind:
    """Liefert den Stack-Kontext aus ``BG_STACK_KIND``.

    Pflicht-Variable: fehlt sie oder ist sie ungueltig, wirft die
    Funktion ``ConfigError`` und der Service startet nicht. Damit ist
    ausgeschlossen, dass ein Stack ohne klare Kennung laeuft — der
    Wert wird von Hard-Guard 1 (``validate_runtime_config``) und vom
    Auto-Login-Trigger (Phase B) zur Sicherheits-Gate-Pruefung gelesen.
    """
    raw = os.environ.get(_STACK_KIND_ENV, "").strip().lower()
    if not raw:
        raise ConfigError(
            f"{_STACK_KIND_ENV} ist nicht gesetzt — Pflicht-Variable, "
            f"erlaubte Werte: {sorted(_STACK_KIND_VALID)}"
        )
    if raw not in _STACK_KIND_VALID:
        raise ConfigError(
            f"{_STACK_KIND_ENV}={raw!r} ist ungueltig — erlaubte Werte: "
            f"{sorted(_STACK_KIND_VALID)} (live/paper)"
        )
    return raw  # type: ignore[return-value]


# ---- Backend-Wahl (cp | tws) ----

BackendKind = Literal["cp", "tws"]

_BACKEND_ENV: Final[str] = "BG_BACKEND"
_BACKEND_DEFAULT: Final[BackendKind] = "cp"
_BACKEND_VALID: Final[frozenset[str]] = frozenset({"cp", "tws"})


def backend_kind() -> BackendKind:
    """Liefert den aktiven Backend-Kanal aus ``BG_BACKEND``.

    Karte 33cb35b1 (TWS-Lifecycle) fuehrt einen Feature-Flag ein, der
    zwischen dem alten cpgateway-Backend (``cp``, Default) und dem neuen
    TWS-API-Backend (``tws``) waehlt.

    Default ist ``cp``, solange ``compose.yaml`` den cpgateway-Service
    haelt — der Wechsel auf ``tws`` als Default ist Teil der Migration-
    Karte 6 (Hard-Cutover). Ungueltige Werte werden mit einer Warning
    auf den Default zurueckgesetzt.
    """
    raw = os.environ.get(_BACKEND_ENV, _BACKEND_DEFAULT).strip().lower()
    if raw not in _BACKEND_VALID:
        logger.warning(
            "ENV %s=%r ist keiner von %s, fallback auf %s",
            _BACKEND_ENV,
            raw,
            sorted(_BACKEND_VALID),
            _BACKEND_DEFAULT,
        )
        return _BACKEND_DEFAULT
    return raw  # type: ignore[return-value]


# ---- Auto-Login (Paper-Stack) ----

_PAPER_AUTO_LOGIN_ENV: Final[str] = "BG_PAPER_AUTO_LOGIN"
_PAPER_USERNAME_ENV: Final[str] = "BG_PAPER_USERNAME"
_PAPER_PASSWORD_ENV: Final[str] = "BG_PAPER_PASSWORD"
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def paper_auto_login_enabled() -> bool:
    """``BG_PAPER_AUTO_LOGIN`` als bool, Default False.

    Truthy: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    Alles andere (inkl. leerer String) wird als False interpretiert.
    """
    raw = os.environ.get(_PAPER_AUTO_LOGIN_ENV, "").strip().lower()
    return raw in _TRUTHY


def paper_credentials() -> tuple[str, str] | None:
    """Liefert ``(username, password)`` wenn beide Env-Vars gesetzt
    und nicht leer sind, sonst ``None``.

    Wird von Hard-Guard 1 (Live darf keine Paper-Creds haben) und vom
    Auto-Login-Trigger gelesen. Niemals loggen — Aufrufer ist fuer
    Redaktion zustaendig.
    """
    user = os.environ.get(_PAPER_USERNAME_ENV, "").strip()
    pwd = os.environ.get(_PAPER_PASSWORD_ENV, "")
    if not user or not pwd:
        return None
    return user, pwd


# ---- TWS read-only (gemeinsamer Schalter gateway <-> tws-Container) ----

_TWS_READ_ONLY_ENV: Final[str] = "BG_TWS_READ_ONLY"


def tws_read_only() -> bool:
    """``BG_TWS_READ_ONLY`` als bool, Default True (sicher).

    Steuert, ob der gateway-seitige ``TWSClient`` die ib_async-Verbindung
    read-only aufbaut (``readonly=True``) und der ``TWSOrdersService``
    Schreib-Operationen (place/modify/cancel-replace) mit ``503
    read_only_api`` ablehnt.

    Dieselbe ENV setzt im tws-Container ``READ_ONLY_API`` (compose.yaml
    ``${BG_TWS_READ_ONLY:-yes}``). Ein gemeinsamer Schalter haelt gateway
    und IB-Gateway konsistent: ohne ihn meldete der tws-Container zwar
    ``READ_ONLY_API=no``, der gateway blieb aber read-only (Default) und
    blockierte jede Order — eine stille Diskrepanz.

    **Nur exakt ``no`` aktiviert write.** Das ist Absicht: der tws-Container
    (gnzsnz/IBC ``ReadOnlyApi``) interpretiert ausschliesslich ``yes``/``no``
    kanonisch — ``off``/``false``/``0`` laesst IBC auf seinem read-only-
    Default und wuerde gateway (write) und tws (read-only) auseinander
    laufen lassen. Alles ausser ``no`` (inkl. unset, leer, Tippfehler)
    bleibt read-only — sicheres Opt-in. **Beide Container muessen denselben
    Wert tragen und zusammen recreatet werden** (build-gateway.sh ruft
    ``up -d gateway tws``); ein gateway-only-Recreate liefe sonst auseinander.
    """
    raw = os.environ.get(_TWS_READ_ONLY_ENV, "yes").strip().lower()
    return raw != "no"


def validate_runtime_config() -> None:
    """Hard-Guard-Pruefung beim Startup.

    Wirft ``ConfigError`` bei jeder Konstellation, die zu einem
    sicherheitsrelevanten Mismatch fuehren wuerde:

    1. ``BG_STACK_KIND`` fehlt oder ungueltig.
    2. ``BG_STACK_KIND=live`` UND ``BG_PAPER_AUTO_LOGIN=1`` —
       Auto-Login darf NIE im Live-Stack laufen.
    3. ``BG_STACK_KIND=live`` UND ``BG_PAPER_USERNAME``/``_PASSWORD``
       gesetzt — Paper-Credentials gehoeren nicht in den Live-Stack,
       auch wenn Auto-Login derzeit aus ist (sonst stiller Drift, der
       beim naechsten Flag-Flip aktiv wird).
    4. ``BG_PAPER_AUTO_LOGIN=1`` ohne ``BG_PAPER_USERNAME``/``_PASSWORD``
       — der Auto-Login wuerde sofort fehlschlagen.
    5. ``BG_STACK_KIND=live`` UND ``BG_TWS_READ_ONLY=no`` — Live-Order-
       Routing ist ausgeschlossen; der Live-Stack bleibt read-only.
    """
    kind = stack_kind()  # wirft selbst, wenn fehlend / ungueltig
    auto_login = paper_auto_login_enabled()
    creds = paper_credentials()

    if kind == "live" and auto_login:
        raise ConfigError(
            "Hard-Guard 1: BG_STACK_KIND=live UND BG_PAPER_AUTO_LOGIN=1 "
            "ist nicht erlaubt. Auto-Login ist Paper-only — der Live-"
            "Stack darf niemals automatisch einloggen."
        )
    if kind == "live" and creds is not None:
        raise ConfigError(
            "Hard-Guard 1: BG_STACK_KIND=live UND BG_PAPER_USERNAME/"
            "BG_PAPER_PASSWORD gesetzt. Paper-Credentials gehoeren "
            "nicht in den Live-Stack — Compose-Trennung pruefen."
        )
    if kind == "live" and not tws_read_only():
        raise ConfigError(
            "Hard-Guard 5: BG_STACK_KIND=live UND BG_TWS_READ_ONLY=no ist "
            "nicht erlaubt. Live-Order-Routing ist in diesem Service-Kontext "
            "ausgeschlossen (AP-14-Constraint 'nur Paper, kein Live-Order') — "
            "der Live-Stack bleibt read-only. Write-Verifikation laeuft "
            "ausschliesslich auf dem Paper-Stack. Bewusstes Live-Order-Routing "
            "waere eine eigene Karte, die diesen Guard explizit lockert."
        )
    if auto_login and creds is None:
        raise ConfigError(
            "BG_PAPER_AUTO_LOGIN=1, aber BG_PAPER_USERNAME oder "
            "BG_PAPER_PASSWORD ist leer. Auto-Login wuerde sofort "
            "fehlschlagen — Credentials in /etc/default/broker-gateway-"
            "paper hinterlegen."
        )


__all__ = [
    "BackendKind",
    "ConfigError",
    "QuotesSource",
    "StackKind",
    "backend_kind",
    "paper_auto_login_enabled",
    "paper_credentials",
    "quotes_source",
    "stack_kind",
    "tws_read_only",
    "validate_runtime_config",
]
