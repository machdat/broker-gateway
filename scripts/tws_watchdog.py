#!/usr/bin/env python3
"""tws-Watchdog: erkennt einen dauerhaft toten tws-Stack, alarmiert per
ntfy-Push und heilt den Paper-Stack automatisch (Karte 53c10ff4).

Hintergrund: Der Live-tws-Stack war von ~22.06. bis 28.06.2026 unbemerkt
down (der IB-Gateway-Java-Listener im tws-Container war tot), weil es
weder Alerting noch Auto-Recovery gab. Der v2.5.3-Socket-Reconnect deckt
nur einen Socket-Abriss ab, nicht den Tod des Listener-Prozesses; der
v2.5.5-Healthcheck *erkennt* den toten Listener (Container -> unhealthy),
loest aber keine Reaktion aus.

Dieser Watchdog laeuft periodisch (systemd-Timer, analog doc-drift) und
prueft pro ueberwachtem Stack zwei Token-freie Signale:

1. **tws-Container-Health** via ``docker inspect`` - seit v2.5.5 spiegelt
   der Healthcheck den IB-Gateway-API-Listener, ``unhealthy`` ist also
   genau der Vorfall-Zustand.
2. **gateway-Erreichbarkeit** via ``GET /v1/health`` - faengt den Fall
   ab, dass der ganze gateway-Container weg ist.

Reaktion bei dauerhaftem Down (>= ``down_threshold`` konsekutive Laeufe):

- **ntfy-Push** auf das konfigurierte Topic (aktiver Handy-Alarm).
- **Paper**: ``ops/recreate-tws.sh paper`` force-recreatet den tws-
  Container (kein 2FA noetig) - genau einmal pro Down-Episode.
- **Live**: NUR Alarm. Das Live-Konto nutzt Mobile-2FA, ein
  vollautomatischer Recovery ist damit ausgeschlossen (Operator
  bestaetigt am Handy, siehe docs/runbooks/tws-recovery.md).

Zustand (konsekutive Down-Zaehler, letzter Alarm-Zeitpunkt, ob in dieser
Episode schon recreatet wurde) liegt in einer JSON-State-Datei, damit der
zustandslose Timer-Lauf Schwellen und Anti-Spam umsetzen kann.

Exit-Codes (fuer systemd-Sichtbarkeit, analog doc-drift):
- ``0`` - alle ueberwachten Stacks up (oder Down noch unter Schwelle).
- ``1`` - mindestens ein Stack dauerhaft down (Alarm ausgeloest).
- ``3`` - Konfigurations-/Voraussetzungsfehler (z.B. ntfy-URL fehlt).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess  # noqa: S404 - docker/recreate-Aufruf ist beabsichtigt
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import httpx  # noqa: E402

EXIT_OK = 0
EXIT_DOWN = 1
EXIT_CONFIG = 3

_DEFAULT_STATE_FILE = Path("/var/lib/broker-gateway/tws-watchdog-state.json")
_DEFAULT_DOWN_THRESHOLD = 2  # konsekutive Laeufe, bevor alarmiert wird
_DEFAULT_REALERT_HOURS = 6.0  # erneuter Alarm fruehestens nach N Stunden
_DEFAULT_RECREATE_SCRIPT = _REPO_ROOT / "ops" / "recreate-tws.sh"
_HTTP_TIMEOUT_S = 8.0
_DOCKER_TIMEOUT_S = 15.0
_RECREATE_TIMEOUT_S = 180.0


class WatchdogError(RuntimeError):
    """Konfigurations- oder Voraussetzungsfehler im Watchdog."""


@dataclass(frozen=True)
class StackConfig:
    """Statische Beschreibung eines ueberwachten Stacks."""

    name: str  # "live" | "paper"
    tws_container: str  # Docker-Container-Name des tws-Service
    health_url: str  # GET .../v1/health
    is_paper: bool  # nur Paper wird auto-recreatet (kein 2FA)


# Default-Topologie auf cma-pi-1 (siehe ops/build-gateway.sh).
_DEFAULT_STACKS: tuple[StackConfig, ...] = (
    StackConfig(
        name="live",
        tws_container="broker-gateway-tws",
        health_url="http://localhost:4000/v1/health",
        is_paper=False,
    ),
    StackConfig(
        name="paper",
        tws_container="broker-gateway-paper-tws",
        health_url="http://localhost:4001/v1/health",
        is_paper=True,
    ),
)


@dataclass
class StackCheck:
    """Ergebnis eines einzelnen Stack-Checks."""

    stack: str
    up: bool
    tws_health: str  # "healthy" | "unhealthy" | "starting" | "missing" | "error"
    gateway_ok: bool
    reason: str


@dataclass
class StackOutcome:
    """Was fuer einen Stack in diesem Lauf passiert ist (fuer Tests/Report)."""

    stack: str
    up: bool
    consecutive_down: int
    alerted: bool = False
    recreated: bool = False
    recovered: bool = False
    reason: str = ""


@dataclass
class WatchdogState:
    """Persistenter Zustand pro Stack zwischen den Timer-Laeufen."""

    # stack -> {"consecutive_down": int, "last_alert": iso|None,
    #           "recreated_this_episode": bool}
    stacks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def for_stack(self, name: str) -> dict[str, Any]:
        return self.stacks.setdefault(
            name,
            {"consecutive_down": 0, "last_alert": None, "recreated_this_episode": False},
        )

    @classmethod
    def load(cls, path: Path) -> "WatchdogState":
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            # Korrupter State darf den Watchdog nicht lahmlegen - frisch starten.
            return cls()
        stacks = raw.get("stacks") if isinstance(raw, dict) else None
        return cls(stacks=stacks if isinstance(stacks, dict) else {})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"stacks": self.stacks}, indent=2, sort_keys=True),
            encoding="utf-8",
        )


# Typ-Aliase fuer die injizierbaren Hooks (Tests reichen Fakes rein).
DockerInspectFn = Callable[[str], str]
HttpGetFn = Callable[[str], bool]
NtfySendFn = Callable[[str, str, str], bool]  # (title, message, priority) -> ok
RecreateFn = Callable[[str], bool]  # (stack_name) -> ok


# ---- Signal-Erfassung ---------------------------------------------------


def _default_docker_inspect(container: str) -> str:
    """Liefert den Health-Status eines Containers via ``docker inspect``.

    Rueckgaben: "healthy" | "unhealthy" | "starting" | "none" | "missing".
    "none" = Container laeuft, hat aber keinen Healthcheck (sollte hier
    nicht vorkommen, wird wie up behandelt). "missing" = Container existiert
    nicht / Docker-Fehler.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [
                "docker",
                "inspect",
                "-f",
                "{{if .State.Health}}{{.State.Health.Status}}"
                "{{else}}{{.State.Status}}{{end}}",
                container,
            ],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return "missing"
    if proc.returncode != 0:
        return "missing"
    status = proc.stdout.strip().lower()
    return status or "missing"


def _default_http_get(url: str) -> bool:
    """True, wenn ``GET url`` mit HTTP 200 antwortet."""
    try:
        resp = httpx.get(url, timeout=_HTTP_TIMEOUT_S)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def check_stack(
    config: StackConfig,
    *,
    docker_inspect: DockerInspectFn,
    http_get: HttpGetFn,
) -> StackCheck:
    """Prueft einen Stack ueber beide Signale.

    Down = tws-Container nicht (healthy|none|running) ODER gateway nicht
    erreichbar. "starting" gilt NICHT als down (Container faehrt gerade
    hoch); erst ein verharrendes unhealthy ueber mehrere Laeufe loest aus.
    """
    health = docker_inspect(config.tws_container)
    gateway_ok = http_get(config.health_url)

    tws_ok = health in ("healthy", "none", "running")
    tws_transient = health == "starting"

    if tws_ok and gateway_ok:
        return StackCheck(config.name, True, health, gateway_ok, "ok")
    if tws_transient:
        # Container faehrt hoch - noch nicht als Down werten.
        return StackCheck(
            config.name, True, health, gateway_ok, "tws startet (transient)"
        )

    reasons = []
    if not tws_ok:
        reasons.append(f"tws-Container={health}")
    if not gateway_ok:
        reasons.append("gateway /v1/health nicht erreichbar")
    return StackCheck(config.name, False, health, gateway_ok, "; ".join(reasons))


# ---- Aktionen -----------------------------------------------------------


def _default_ntfy_send(
    ntfy_url: str,
) -> NtfySendFn:
    """Baut einen ntfy-Sender, der gegen ``ntfy_url`` postet."""

    def _send(title: str, message: str, priority: str) -> bool:
        try:
            resp = httpx.post(
                ntfy_url,
                content=message.encode("utf-8"),
                headers={
                    "Title": title,
                    "Priority": priority,  # "urgent" | "high" | "default"
                    "Tags": "warning,broker-gateway",
                },
                timeout=_HTTP_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            print(f"WARNUNG: ntfy-Push fehlgeschlagen: {exc}", file=sys.stderr)
            return False
        if resp.status_code not in (200, 201):
            print(
                f"WARNUNG: ntfy antwortete mit Status {resp.status_code}",
                file=sys.stderr,
            )
            return False
        return True

    return _send


def _default_recreate(script_path: Path) -> RecreateFn:
    """Baut einen Recreate-Runner, der ``ops/recreate-tws.sh <stack>`` ruft."""

    def _run(stack_name: str) -> bool:
        if not script_path.is_file():
            print(
                f"WARNUNG: Recreate-Skript {script_path} nicht gefunden.",
                file=sys.stderr,
            )
            return False
        try:
            proc = subprocess.run(  # noqa: S603
                ["bash", str(script_path), stack_name],
                capture_output=True,
                text=True,
                timeout=_RECREATE_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"WARNUNG: Recreate fehlgeschlagen: {exc}", file=sys.stderr)
            return False
        if proc.returncode != 0:
            print(
                f"WARNUNG: Recreate exit={proc.returncode}: {proc.stderr.strip()}",
                file=sys.stderr,
            )
            return False
        return True

    return _run


# ---- Kern-Logik ---------------------------------------------------------


def _should_alert(
    stack_state: dict[str, Any], now: datetime, realert_hours: float
) -> bool:
    """True, wenn (erstmals die Schwelle erreicht) ODER (Re-Alert faellig)."""
    last_alert_raw = stack_state.get("last_alert")
    if not last_alert_raw:
        return True
    try:
        last_alert = datetime.fromisoformat(last_alert_raw)
        if last_alert.tzinfo is None:
            # Defensiv gegen manuell editierte naive Timestamps - der
            # Watchdog selbst schreibt immer UTC-aware.
            last_alert = last_alert.replace(tzinfo=timezone.utc)
        elapsed_h = (now - last_alert).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return True
    return elapsed_h >= realert_hours


def process_stack(
    config: StackConfig,
    check: StackCheck,
    state: WatchdogState,
    *,
    now: datetime,
    down_threshold: int,
    realert_hours: float,
    auto_recreate_paper: bool,
    ntfy_send: NtfySendFn,
    recreate: RecreateFn,
) -> StackOutcome:
    """Aktualisiert State und loest Alarm/Recreate fuer einen Stack aus."""
    st = state.for_stack(config.name)

    if check.up:
        recovered = st.get("consecutive_down", 0) >= down_threshold
        reset = True
        if recovered:
            sent = ntfy_send(
                f"broker-gateway {config.name}: tws WIEDER OK",
                f"Der {config.name}-tws-Stack ist wieder healthy.",
                "default",
            )
            # Push fehlgeschlagen -> State NICHT zuruecksetzen, damit der
            # naechste Lauf den Recovery-Push erneut versucht (analog zum
            # Down-Alarm, der last_alert nur bei Erfolg setzt).
            reset = sent
        if reset:
            st["consecutive_down"] = 0
            st["last_alert"] = None
            st["recreated_this_episode"] = False
        return StackOutcome(
            stack=config.name, up=True,
            consecutive_down=int(st.get("consecutive_down", 0)),
            recovered=recovered, reason=check.reason,
        )

    # --- Stack ist down ---
    st["consecutive_down"] = int(st.get("consecutive_down", 0)) + 1
    consecutive = st["consecutive_down"]
    outcome = StackOutcome(
        stack=config.name, up=False, consecutive_down=consecutive, reason=check.reason,
    )

    if consecutive < down_threshold:
        # Noch unter Schwelle - nur zaehlen, kein Alarm (Schutz gegen Blips).
        return outcome

    # Schwelle erreicht: Paper heilen (einmal pro Episode), dann/sonst alarmieren.
    if config.is_paper and auto_recreate_paper and not st.get(
        "recreated_this_episode"
    ):
        if recreate(config.name):
            st["recreated_this_episode"] = True
            outcome.recreated = True

    if _should_alert(st, now, realert_hours):
        prio = "urgent" if not config.is_paper else "high"
        if outcome.recreated:
            action = "Paper-tws wurde automatisch force-recreatet."
        elif not config.is_paper:
            action = "Manueller Recovery noetig (2FA am Handy) - siehe Runbook."
        elif st.get("recreated_this_episode"):
            # Recreate lief in dieser Episode schon (Latch), half aber nicht
            # - NICHT als 'fehlgeschlagen' melden, sonst sucht der Operator
            # am falschen Ort (Ursache liegt tiefer, z.B. haengender Login).
            action = "Auto-Recreate lief bereits, tws bleibt down - bitte manuell pruefen."
        else:
            action = "Auto-Recreate nicht moeglich/fehlgeschlagen - bitte pruefen."
        sent = ntfy_send(
            f"broker-gateway {config.name}: tws DOWN",
            f"{config.name}-tws down seit {consecutive} Laeufen "
            f"({check.reason}). {action}",
            prio,
        )
        if sent:
            st["last_alert"] = now.isoformat()
            outcome.alerted = True

    return outcome


def run(
    *,
    stacks: tuple[StackConfig, ...],
    state_file: Path,
    now: datetime,
    down_threshold: int,
    realert_hours: float,
    auto_recreate_paper: bool,
    docker_inspect: DockerInspectFn,
    http_get: HttpGetFn,
    ntfy_send: NtfySendFn,
    recreate: RecreateFn,
) -> list[StackOutcome]:
    """Fuehrt einen Watchdog-Lauf ueber alle Stacks aus."""
    state = WatchdogState.load(state_file)
    outcomes: list[StackOutcome] = []
    for config in stacks:
        check = check_stack(config, docker_inspect=docker_inspect, http_get=http_get)
        outcome = process_stack(
            config,
            check,
            state,
            now=now,
            down_threshold=down_threshold,
            realert_hours=realert_hours,
            auto_recreate_paper=auto_recreate_paper,
            ntfy_send=ntfy_send,
            recreate=recreate,
        )
        outcomes.append(outcome)
        status = "UP" if outcome.up else f"DOWN (x{outcome.consecutive_down})"
        flags = []
        if outcome.alerted:
            flags.append("alerted")
        if outcome.recreated:
            flags.append("recreated")
        if outcome.recovered:
            flags.append("recovered")
        suffix = f" [{','.join(flags)}]" if flags else ""
        print(f"[{config.name}] {status}: {outcome.reason}{suffix}")
    state.save(state_file)
    return outcomes


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tws_watchdog",
        description=(
            "Watchdog fuer die tws-Stacks: alarmiert per ntfy bei dauerhaftem "
            "Down und force-recreatet den Paper-tws automatisch."
        ),
    )
    p.add_argument(
        "--ntfy-url",
        default=os.environ.get("BG_WATCHDOG_NTFY_URL", ""),
        help="ntfy-Topic-URL (z.B. https://ntfy.sh/<topic>). Env: BG_WATCHDOG_NTFY_URL.",
    )
    p.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ.get("BG_WATCHDOG_STATE_FILE", str(_DEFAULT_STATE_FILE))),
        help=f"State-JSON-Pfad (default {_DEFAULT_STATE_FILE}).",
    )
    p.add_argument(
        "--down-threshold",
        type=int,
        default=int(os.environ.get("BG_WATCHDOG_DOWN_THRESHOLD", _DEFAULT_DOWN_THRESHOLD)),
        help=f"Konsekutive Down-Laeufe vor Alarm (default {_DEFAULT_DOWN_THRESHOLD}).",
    )
    p.add_argument(
        "--realert-hours",
        type=float,
        default=float(os.environ.get("BG_WATCHDOG_REALERT_HOURS", _DEFAULT_REALERT_HOURS)),
        help=f"Re-Alert fruehestens nach N Stunden (default {_DEFAULT_REALERT_HOURS}).",
    )
    p.add_argument(
        "--no-auto-recreate-paper",
        action="store_true",
        help="Paper-Auto-Recreate deaktivieren (nur alarmieren).",
    )
    p.add_argument(
        "--recreate-script",
        type=Path,
        default=_DEFAULT_RECREATE_SCRIPT,
        help=f"Pfad zu recreate-tws.sh (default {_DEFAULT_RECREATE_SCRIPT}).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.ntfy_url:
        print(
            "VORAUSSETZUNG FEHLT: --ntfy-url bzw. BG_WATCHDOG_NTFY_URL ist leer.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    outcomes = run(
        stacks=_DEFAULT_STACKS,
        state_file=args.state_file,
        now=datetime.now(timezone.utc),
        down_threshold=args.down_threshold,
        realert_hours=args.realert_hours,
        auto_recreate_paper=not args.no_auto_recreate_paper,
        docker_inspect=_default_docker_inspect,
        http_get=_default_http_get,
        ntfy_send=_default_ntfy_send(args.ntfy_url),
        recreate=_default_recreate(args.recreate_script),
    )
    any_down = any(
        (not o.up) and o.consecutive_down >= args.down_threshold for o in outcomes
    )
    return EXIT_DOWN if any_down else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
