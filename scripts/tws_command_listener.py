#!/usr/bin/env python3
"""tws-Command-Listener: ferngesteuerter Live-tws-force-recreate via ntfy.

Karte a529e59a (AP-15). Der Operator kann den Live-tws-Restart vom Handy aus
anstossen, ohne SSH/Terminal: die Pi ist nicht aus dem Internet erreichbar,
lauscht aber ausgehend auf einem oeffentlichen ntfy.sh-Command-Topic.

Ablauf (Bestaetigungs-Round-Trip statt statischem Shared-Secret):

1. Eine Nachricht ``recreate-live`` im Command-Topic (z.B. per Action-Button
   des Watchdog-Alarms) triggert einen server-seitigen One-Time-Nonce.
2. Der Dienst pusht auf das Status-Topic (= Watchdog-Alarm-Topic, dort ist
   der Operator schon subscribed) eine Nachricht mit einem
   ``Jetzt neu starten``-Action-Button, der ``confirm <nonce>`` zurueck ins
   Command-Topic postet.
3. Erst ``confirm <nonce>`` (Nonce-Match, nicht abgelaufen) loest GENAU EINEN
   ``ops/recreate-tws-live.sh`` aus (Opt-in BG_ALLOW_LIVE_RECREATE=yes).
4. Danach pollt der Dienst die tws-Container-Health und meldet das Ergebnis.

**Hard-Constraint:** Die eigentliche 2FA-Genehmigung bleibt der IB-Key-Tap in
der IBKR-App - dieser Dienst triggert nur den Recreate und meldet Status.

**Bedrohungsmodell (bewusst offen gehalten):** Das ntfy.sh-Command-Topic ist
oeffentlich. Der Nonce-Round-Trip bindet ein ``confirm`` an einen zuvor
angeforderten Request und schuetzt vor versehentlichem Doppel-Trigger, ist
gegen einen Mitleser des Topics aber KEIN Vollschutz. Die inhaerente
Schadensgrenze: ein unautorisierter Recreate kann den Login nicht abschliessen
(IB-Key-Tap noetig), Worst Case ist 2FA-Push-Spam - dagegen wirkt der
Cooldown. Mitigation: zufaellige Topic-Namen, Command- und Status-Topic NICHT
identisch waehlen (siehe docs/04-security.md).

Die Kern-Logik ist - wie beim tws_watchdog-Vorbild - ueber injizierbare Hooks
und ein explizit uebergebenes ``now`` voll ohne Netzwerk/Docker testbar.

Exit-Codes:
- ``0`` - sauberer Shutdown (SIGTERM/SIGINT).
- ``3`` - Konfigurationsfehler (Command-/Status-Topic-URL fehlt).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import signal
import subprocess  # noqa: S404 - Aufruf von recreate-tws-live.sh ist beabsichtigt
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import httpx  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 3

_DEFAULT_CONFIRM_TTL_S = 120.0
_DEFAULT_COOLDOWN_S = 300.0
_DEFAULT_HEALTH_WAIT_S = 180.0  # deckt das 2FA-Bestaetigungsfenster ab
_DEFAULT_HEALTH_POLL_INTERVAL_S = 10.0
_DEFAULT_TWS_CONTAINER = "broker-gateway-tws"  # Live-Stack
_DEFAULT_RECREATE_SCRIPT = _REPO_ROOT / "ops" / "recreate-tws-live.sh"

_HTTP_TIMEOUT_S = 8.0
_DOCKER_TIMEOUT_S = 15.0
_RECREATE_TIMEOUT_S = 60.0
_STREAM_INITIAL_BACKOFF_S = 1.0
_STREAM_MAX_BACKOFF_S = 60.0
# Endlicher Read-Timeout > ntfy-Keepalive (~45s auf dem /json-Stream): ohne
# ihn blockiert ein still abgebrochener TCP-Stream iter_lines() dauerhaft und
# der Reconnect greift nie - der Dienst waere still tot (Memory
# feedback_sse_reconnect_pattern).
_STREAM_READ_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class ListenerConfig:
    """Statische Konfiguration des Command-Listeners."""

    command_topic_url: str  # hier lauscht der Dienst (recreate-live/confirm)
    status_topic_url: str  # hierher gehen alle Rueckmeldungen (= Alarm-Topic)
    confirm_ttl_s: float
    cooldown_s: float
    health_wait_s: float
    health_poll_interval_s: float
    tws_container: str


@dataclass
class ListenerState:
    """Fluechtiger Zustand: der eine offene Nonce und das Cooldown-Fenster."""

    nonce: str | None = None
    nonce_expires: float | None = None
    cooldown_until: float | None = None


# Typ-Aliase fuer die injizierbaren Hooks (Tests reichen Fakes rein).
NtfySendFn = Callable[..., bool]  # (title, message, priority, actions=None) -> ok
RecreateFn = Callable[[], bool]  # loest recreate-tws-live.sh aus -> ok
DockerInspectFn = Callable[[str], str]  # container -> Health-Status
SleepFn = Callable[[float], None]
NonceFn = Callable[[], str]


# ---- Reine Helfer (voll testbar) ----------------------------------------


def parse_stream_line(line: str) -> str | None:
    """Extrahiert das ``message``-Feld einer ntfy-Stream-JSON-Zeile.

    ntfy liefert unter ``GET .../json`` newline-getrennte JSON-Objekte mit
    einem ``event``-Feld. Nur ``event == "message"`` traegt einen Nutz-Befehl;
    ``open``/``keepalive`` (und alles Uebrige) werden zu ``None`` gefiltert.
    """
    if not line or not line.strip():
        return None
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(event, dict) or event.get("event") != "message":
        return None
    message = event.get("message")
    if isinstance(message, str) and message.strip():
        return message
    return None


def build_confirm_action(command_topic_url: str, nonce: str) -> str:
    """ntfy-Actions-Header: ein http-Button, der ``confirm <nonce>`` postet.

    Das Button-Label ist bewusst umlautfrei (HTTP-Header sind latin-1) - der
    ausfuehrliche deutsche Text mit Umlauten steht im Message-Body.
    """
    return (
        f"http, Jetzt neu starten, {command_topic_url}, "
        f"method=POST, body=confirm {nonce}, clear=true"
    )


# ---- Kern-Logik ---------------------------------------------------------


class CommandListener:
    """Verarbeitet Command-Topic-Nachrichten und steuert den Recreate-Loop.

    Titel/Action-Labels der ntfy-Pushes sind bewusst umlautfrei (HTTP-Header),
    die vollen deutschen Texte mit korrekten Umlauten stehen im Message-Body.
    """

    def __init__(
        self,
        config: ListenerConfig,
        *,
        ntfy_send: NtfySendFn,
        recreate: RecreateFn,
        docker_inspect: DockerInspectFn,
        sleep: SleepFn,
        gen_nonce: NonceFn,
        state: ListenerState | None = None,
    ) -> None:
        self.config = config
        self.ntfy_send = ntfy_send
        self.recreate = recreate
        self.docker_inspect = docker_inspect
        self.sleep = sleep
        self.gen_nonce = gen_nonce
        self.state = state or ListenerState()

    # -- Eingang --

    def handle_message(self, message: str, now: float) -> None:
        """Reagiert auf einen eingegangenen Command-Topic-Befehl."""
        command = message.strip()
        if command == "recreate-live":
            self._on_trigger(now)
        elif command.startswith("confirm "):
            self._on_confirm(command[len("confirm "):].strip(), now)
        # alles Uebrige wird bewusst ignoriert

    # -- Schritte --

    def _on_trigger(self, now: float) -> None:
        if self.state.cooldown_until is not None and now < self.state.cooldown_until:
            remaining = int(self.state.cooldown_until - now)
            self._push(
                "Cooldown aktiv",
                f"Live-Restart ist erst in {remaining}s wieder ausloesbar "
                f"(genau EIN force-recreate pro Fenster).",
                "default",
            )
            return
        # Neuer One-Time-Nonce; ein frischer Trigger ueberschreibt den alten.
        nonce = self.gen_nonce()
        self.state.nonce = nonce
        self.state.nonce_expires = now + self.config.confirm_ttl_s
        ttl = int(self.config.confirm_ttl_s)
        self._push(
            "Live-Restart-Anforderung",
            f"Nonce {nonce} gueltig {ttl}s. Zum Ausfuehren unten bestaetigen - "
            f"danach IB Key am Handy freigeben.",
            "high",
            build_confirm_action(self.config.command_topic_url, nonce),
        )

    def _on_confirm(self, nonce: str, now: float) -> None:
        valid = (
            self.state.nonce is not None
            and nonce == self.state.nonce
            and self.state.nonce_expires is not None
            and now < self.state.nonce_expires
        )
        if not valid:
            self._push(
                "Bestaetigung abgelehnt",
                "Der Nonce ist ungueltig oder abgelaufen - es wurde KEIN "
                "Recreate ausgefuehrt. Bei Bedarf erneut anfordern.",
                "default",
            )
            return
        # One-Time: Nonce sofort verbrauchen, bevor der Recreate laeuft.
        self.state.nonce = None
        self.state.nonce_expires = None
        self._do_recreate(now)

    def _do_recreate(self, now: float) -> None:
        # Cooldown ab jetzt - schuetzt vor konkurrierenden 2FA-Pushes, auch
        # wenn der Recreate scheitert (er kann bereits einen Push angestossen
        # haben). Runbook-Disziplin: genau EIN force-recreate pro Fenster.
        self.state.cooldown_until = now + self.config.cooldown_s
        self._push(
            "Recreate gestartet",
            "Live-tws wird neu erstellt - jetzt den IB-Key-Push in der "
            "IBKR-App bestaetigen.",
            "high",
        )
        if not self.recreate():
            self._push(
                "Recreate fehlgeschlagen",
                "ops/recreate-tws-live.sh meldete einen Fehler - bitte manuell "
                "pruefen (siehe docs/runbooks/tws-recovery.md).",
                "urgent",
            )
            return
        if self._poll_health():
            self._push(
                "tws wieder OK",
                "Live-tws ist nach dem Recreate wieder connected und healthy.",
                "default",
            )
        else:
            self._push(
                "tws weiterhin down",
                "Live-tws ist nach dem Recreate nicht healthy geworden - "
                "wurde der IB-Key-Push bestaetigt? Sonst Runbook.",
                "urgent",
            )

    def _poll_health(self) -> bool:
        """Pollt die tws-Container-Health iterationsbasiert bis healthy/Timeout."""
        interval = self.config.health_poll_interval_s
        polls = (
            max(1, math.ceil(self.config.health_wait_s / interval))
            if interval > 0
            else 1
        )
        for i in range(polls):
            if self.docker_inspect(self.config.tws_container) == "healthy":
                return True
            if i < polls - 1:
                self.sleep(interval)
        return False

    def _push(
        self, title: str, message: str, priority: str, actions: str | None = None
    ) -> None:
        self.ntfy_send(title, message, priority, actions)


# ---- Default-Hooks (I/O) ------------------------------------------------


def _default_ntfy_send(status_topic_url: str) -> NtfySendFn:
    """Baut einen ntfy-Sender, der ans Status-Topic postet (optional mit Action)."""

    def _send(
        title: str, message: str, priority: str, actions: str | None = None
    ) -> bool:
        headers = {
            "Title": title,
            "Priority": priority,
            "Tags": "broker-gateway",
        }
        if actions:
            headers["Actions"] = actions
        try:
            resp = httpx.post(
                status_topic_url,
                content=message.encode("utf-8"),
                headers=headers,
                timeout=_HTTP_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            print(f"WARNUNG: ntfy-Push fehlgeschlagen: {exc}", file=sys.stderr)
            return False
        if resp.status_code not in (200, 201):
            print(f"WARNUNG: ntfy antwortete mit Status {resp.status_code}", file=sys.stderr)
            return False
        return True

    return _send


def _default_recreate(script_path: Path, live_repo_dir: str | None) -> RecreateFn:
    """Baut einen Runner, der recreate-tws-live.sh mit gesetztem Opt-in ruft."""

    def _run() -> bool:
        if not script_path.is_file():
            print(f"WARNUNG: Recreate-Skript {script_path} nicht gefunden.", file=sys.stderr)
            return False
        env = dict(os.environ)
        env["BG_ALLOW_LIVE_RECREATE"] = "yes"
        if live_repo_dir:
            env["BG_LIVE_REPO_DIR"] = live_repo_dir
        try:
            proc = subprocess.run(  # noqa: S603
                ["bash", str(script_path)],
                capture_output=True,
                text=True,
                timeout=_RECREATE_TIMEOUT_S,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"WARNUNG: Recreate-Aufruf fehlgeschlagen: {exc}", file=sys.stderr)
            return False
        if proc.returncode != 0:
            print(
                f"WARNUNG: recreate-tws-live.sh exit={proc.returncode}: "
                f"{proc.stderr.strip()}",
                file=sys.stderr,
            )
            return False
        return True

    return _run


def _default_docker_inspect(container: str) -> str:
    """Health-Status eines Containers via ``docker inspect`` (Token-frei)."""
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
    return proc.stdout.strip().lower() or "missing"


# ---- Subscribe-Loop (I/O) -----------------------------------------------


class _StopFlag:
    """Von Signal-Handlern gesetztes Stop-Flag; truthy sobald ausgeloest."""

    def __init__(self) -> None:
        self._stop = False

    def __call__(self, *_args: object) -> None:
        self._stop = True

    def __bool__(self) -> bool:
        return self._stop


StreamLinesFn = Callable[[str], Iterator[str]]


def _default_stream_lines(read_timeout_s: float) -> StreamLinesFn:
    """Baut einen Stream-Opener: httpx-GET mit ENDLICHEM Read-Timeout.

    Der endliche Read-Timeout (> ntfy-Keepalive) ist essenziell - ohne ihn
    wuerde ein still abgebrochener Stream iter_lines() dauerhaft blockieren
    und der Reconnect nie greifen. Ein ReadTimeout kommt als httpx.HTTPError
    hoch und laesst den Backoff/Reconnect im Loop greifen.
    """
    timeout = httpx.Timeout(
        connect=_HTTP_TIMEOUT_S,
        read=read_timeout_s,
        write=_HTTP_TIMEOUT_S,
        pool=_HTTP_TIMEOUT_S,
    )

    def _open(url: str) -> Iterator[str]:
        with httpx.stream("GET", url, timeout=timeout) as resp:
            resp.raise_for_status()
            yield from resp.iter_lines()

    return _open


def _subscribe_loop(
    config: ListenerConfig,
    listener: CommandListener,
    *,
    stop: _StopFlag,
    sleep: SleepFn,
    monotonic: Callable[[], float],
    stream_lines: StreamLinesFn,
    initial_backoff: float = _STREAM_INITIAL_BACKOFF_S,
    max_backoff: float = _STREAM_MAX_BACKOFF_S,
) -> None:
    """Abonniert das Command-Topic und reicht Nachrichten an den Listener.

    ntfy-Streams brechen periodisch ab - das ist normal. Bei jedem Abriss (auch
    bei einem Read-Timeout auf einem still toten Stream) wird mit exponentiellem
    Backoff neu verbunden, damit der Dienst nicht stirbt.
    """
    url = config.command_topic_url.rstrip("/") + "/json"
    backoff = initial_backoff
    while not stop:
        try:
            for line in stream_lines(url):
                if stop:
                    break
                message = parse_stream_line(line)
                if message is not None:
                    listener.handle_message(message, monotonic())
            backoff = initial_backoff  # sauberer Stream-Abschluss -> Reset
        except (httpx.HTTPError, OSError) as exc:
            print(
                f"WARNUNG: Command-Stream abgerissen ({exc}) - "
                f"Reconnect in {backoff:.0f}s.",
                file=sys.stderr,
            )
        if stop:
            break
        sleep(backoff)
        backoff = min(max_backoff, backoff * 2)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tws_command_listener",
        description=(
            "Always-on Listener fuer den ferngesteuerten Live-tws-force-recreate "
            "via ntfy (Bestaetigungs-Round-Trip)."
        ),
    )
    p.add_argument(
        "--command-topic-url",
        default=os.environ.get("BG_CMD_COMMAND_TOPIC_URL", ""),
        help="ntfy-Command-Topic-URL. Env: BG_CMD_COMMAND_TOPIC_URL.",
    )
    p.add_argument(
        "--status-topic-url",
        default=os.environ.get("BG_CMD_STATUS_TOPIC_URL", ""),
        help="ntfy-Status-Topic-URL (= Watchdog-Alarm-Topic). Env: BG_CMD_STATUS_TOPIC_URL.",
    )
    p.add_argument(
        "--confirm-ttl-s",
        type=float,
        default=float(os.environ.get("BG_CMD_CONFIRM_TTL_S", _DEFAULT_CONFIRM_TTL_S)),
        help=f"Gueltigkeit eines Nonce in Sekunden (default {int(_DEFAULT_CONFIRM_TTL_S)}).",
    )
    p.add_argument(
        "--cooldown-s",
        type=float,
        default=float(os.environ.get("BG_CMD_COOLDOWN_S", _DEFAULT_COOLDOWN_S)),
        help=f"Cooldown nach einem Recreate in Sekunden (default {int(_DEFAULT_COOLDOWN_S)}).",
    )
    p.add_argument(
        "--health-wait-s",
        type=float,
        default=float(os.environ.get("BG_CMD_HEALTH_WAIT_S", _DEFAULT_HEALTH_WAIT_S)),
        help=f"Max. Wartezeit auf tws=healthy nach Recreate (default {int(_DEFAULT_HEALTH_WAIT_S)}).",
    )
    p.add_argument(
        "--health-poll-interval-s",
        type=float,
        default=float(
            os.environ.get(
                "BG_CMD_HEALTH_POLL_INTERVAL_S", _DEFAULT_HEALTH_POLL_INTERVAL_S
            )
        ),
        help=f"Poll-Intervall der Health-Pruefung (default {int(_DEFAULT_HEALTH_POLL_INTERVAL_S)}).",
    )
    p.add_argument(
        "--tws-container",
        default=os.environ.get("BG_CMD_TWS_CONTAINER", _DEFAULT_TWS_CONTAINER),
        help=f"tws-Container-Name (default {_DEFAULT_TWS_CONTAINER}).",
    )
    p.add_argument(
        "--live-repo-dir",
        default=os.environ.get("BG_LIVE_REPO_DIR", ""),
        help="Live-Repo-Pfad, an recreate-tws-live.sh durchgereicht. Env: BG_LIVE_REPO_DIR.",
    )
    p.add_argument(
        "--recreate-script",
        type=Path,
        default=_DEFAULT_RECREATE_SCRIPT,
        help=f"Pfad zu recreate-tws-live.sh (default {_DEFAULT_RECREATE_SCRIPT}).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.command_topic_url or not args.status_topic_url:
        print(
            "VORAUSSETZUNG FEHLT: --command-topic-url/--status-topic-url bzw. "
            "BG_CMD_COMMAND_TOPIC_URL/BG_CMD_STATUS_TOPIC_URL sind leer.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    config = ListenerConfig(
        command_topic_url=args.command_topic_url,
        status_topic_url=args.status_topic_url,
        confirm_ttl_s=args.confirm_ttl_s,
        cooldown_s=args.cooldown_s,
        health_wait_s=args.health_wait_s,
        health_poll_interval_s=args.health_poll_interval_s,
        tws_container=args.tws_container,
    )
    listener = CommandListener(
        config,
        ntfy_send=_default_ntfy_send(config.status_topic_url),
        recreate=_default_recreate(args.recreate_script, args.live_repo_dir or None),
        docker_inspect=_default_docker_inspect,
        sleep=time.sleep,
        gen_nonce=lambda: secrets.token_hex(3),
    )

    stop = _StopFlag()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(
        f"[command-listener] lausche auf Command-Topic; Status-Feedback -> "
        f"{config.status_topic_url}",
        flush=True,
    )
    _subscribe_loop(
        config,
        listener,
        stop=stop,
        sleep=time.sleep,
        monotonic=time.monotonic,
        stream_lines=_default_stream_lines(_STREAM_READ_TIMEOUT_S),
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
