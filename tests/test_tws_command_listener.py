"""Tests fuer scripts/tws_command_listener.py (Karte a529e59a, AP-15).

Der Listener abonniert ein ntfy-Command-Topic und loest nach einem
Bestaetigungs-Round-Trip (One-Time-Nonce) genau EINEN Live-tws-force-recreate
aus. Die Kern-Logik ist - wie beim tws_watchdog-Vorbild - ueber injizierbare
Hooks (ntfy_send, recreate, docker_inspect, sleep, gen_nonce) und ein
explizit uebergebenes ``now`` voll ohne Netzwerk/Docker testbar.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import tws_command_listener as tcl  # noqa: E402


# ---- Test-Doubles -------------------------------------------------------


class _Ntfy:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str, str, str | None]] = []

    def __call__(
        self, title: str, message: str, priority: str, actions: str | None = None
    ) -> bool:
        self.calls.append((title, message, priority, actions))
        return self.ok

    def blob(self, idx: int) -> str:
        t, m, _p, _a = self.calls[idx]
        return f"{t}\n{m}".lower()


class _Recreate:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.count = 0

    def __call__(self) -> bool:
        self.count += 1
        return self.ok


class _Docker:
    """Liefert die uebergebenen Health-Stati der Reihe nach, wiederholt den letzten."""

    def __init__(self, *statuses: str) -> None:
        self._statuses = list(statuses) or ["unhealthy"]
        self.calls = 0

    def __call__(self, container: str) -> str:
        idx = min(self.calls, len(self._statuses) - 1)
        self.calls += 1
        return self._statuses[idx]


_BASE_CONFIG = None  # lazily built in _make (nach Import verfuegbar)


def _make(
    *,
    ntfy: _Ntfy,
    recreate: _Recreate,
    docker: _Docker | None = None,
    sleep=None,
    nonce: str = "abc123",
    **cfg_overrides,
) -> "tcl.CommandListener":
    config = tcl.ListenerConfig(
        command_topic_url="https://ntfy.sh/cmd-xyz",
        status_topic_url="https://ntfy.sh/status-xyz",
        confirm_ttl_s=120.0,
        cooldown_s=300.0,
        health_wait_s=30.0,
        health_poll_interval_s=10.0,
        tws_container="broker-gateway-tws",
    )
    if cfg_overrides:
        config = replace(config, **cfg_overrides)
    return tcl.CommandListener(
        config,
        ntfy_send=ntfy,
        recreate=recreate,
        docker_inspect=docker or _Docker("healthy"),
        sleep=sleep or (lambda _s: None),
        gen_nonce=lambda: nonce,
    )


# ---- Bestaetigungs-Round-Trip ------------------------------------------


def test_trigger_creates_nonce_and_action_without_recreate():
    ntfy, rec = _Ntfy(), _Recreate()
    lis = _make(ntfy=ntfy, recreate=rec)

    lis.handle_message("recreate-live", now=1000.0)

    assert rec.count == 0, "vor der Bestaetigung darf NICHT recreatet werden"
    assert len(ntfy.calls) == 1
    _t, msg, _prio, actions = ntfy.calls[0]
    assert "abc123" in msg, "Nonce muss im Bestaetigungs-Push stehen"
    assert actions is not None, "Status-Push braucht einen Bestaetigen-Action-Button"
    assert "confirm abc123" in actions
    assert "cmd-xyz" in actions, "Action-Button postet ins Command-Topic"


def test_confirm_valid_nonce_triggers_single_recreate():
    ntfy, rec = _Ntfy(), _Recreate()
    lis = _make(ntfy=ntfy, recreate=rec, docker=_Docker("healthy"))

    lis.handle_message("recreate-live", now=1000.0)
    lis.handle_message("confirm abc123", now=1010.0)

    assert rec.count == 1, "genau EIN Recreate nach gueltiger Bestaetigung"
    # Reihenfolge: [Bestaetigen-Prompt, 'Recreate laeuft', Abschluss-OK]
    assert len(ntfy.calls) == 3
    assert ntfy.calls[0][3] is not None  # Prompt traegt Action
    assert "recreate" in ntfy.blob(1)  # 'Recreate laeuft ...'
    assert ntfy.calls[1][3] is None  # Fortschritts-Pushes ohne Button


def test_confirm_wrong_nonce_no_recreate():
    ntfy, rec = _Ntfy(), _Recreate()
    lis = _make(ntfy=ntfy, recreate=rec)

    lis.handle_message("recreate-live", now=1000.0)
    lis.handle_message("confirm deadbeef", now=1010.0)

    assert rec.count == 0
    assert "ungueltig" in ntfy.blob(-1) or "abgelaufen" in ntfy.blob(-1)


def test_confirm_expired_nonce_no_recreate():
    ntfy, rec = _Ntfy(), _Recreate()
    lis = _make(ntfy=ntfy, recreate=rec, confirm_ttl_s=120.0)

    lis.handle_message("recreate-live", now=1000.0)  # gueltig bis 1120
    lis.handle_message("confirm abc123", now=1200.0)  # abgelaufen

    assert rec.count == 0
    assert "ungueltig" in ntfy.blob(-1) or "abgelaufen" in ntfy.blob(-1)


def test_nonce_is_one_time():
    ntfy, rec = _Ntfy(), _Recreate()
    lis = _make(ntfy=ntfy, recreate=rec)

    lis.handle_message("recreate-live", now=1000.0)
    lis.handle_message("confirm abc123", now=1010.0)  # verbraucht den Nonce
    lis.handle_message("confirm abc123", now=1020.0)  # darf nichts mehr ausloesen

    assert rec.count == 1


# ---- Cooldown -----------------------------------------------------------


def test_second_trigger_within_cooldown_is_ignored():
    ntfy, rec = _Ntfy(), _Recreate()
    lis = _make(ntfy=ntfy, recreate=rec, cooldown_s=300.0)

    lis.handle_message("recreate-live", now=1000.0)
    lis.handle_message("confirm abc123", now=1010.0)  # Recreate -> Cooldown bis 1310
    calls_before = len(ntfy.calls)

    lis.handle_message("recreate-live", now=1100.0)  # innerhalb Cooldown

    assert rec.count == 1, "kein zweiter Recreate im Cooldown"
    assert len(ntfy.calls) == calls_before + 1
    _t, _m, _p, actions = ntfy.calls[-1]
    assert actions is None, "Cooldown-Hinweis erzeugt keinen neuen Nonce/Button"
    assert "cooldown" in ntfy.blob(-1)


def test_trigger_after_cooldown_expired_works_again():
    ntfy, rec = _Ntfy(), _Recreate()
    lis = _make(ntfy=ntfy, recreate=rec, cooldown_s=300.0)

    lis.handle_message("recreate-live", now=1000.0)
    lis.handle_message("confirm abc123", now=1010.0)  # Cooldown bis 1310
    lis.handle_message("recreate-live", now=1400.0)  # nach Cooldown
    lis.handle_message("confirm abc123", now=1410.0)

    assert rec.count == 2


# ---- Health-Poll --------------------------------------------------------


def test_health_poll_success_sends_ok_push():
    ntfy, rec = _Ntfy(), _Recreate()
    docker = _Docker("unhealthy", "unhealthy", "healthy")
    sleeps: list[float] = []
    lis = _make(
        ntfy=ntfy, recreate=rec, docker=docker, sleep=sleeps.append,
        health_wait_s=30.0, health_poll_interval_s=10.0,
    )

    lis.handle_message("recreate-live", now=1000.0)
    lis.handle_message("confirm abc123", now=1010.0)

    assert rec.count == 1
    assert docker.calls == 3
    blob = ntfy.blob(-1)
    assert "ok" in blob or "healthy" in blob or "connected" in blob
    assert "down" not in blob


def test_health_poll_timeout_sends_down_push():
    ntfy, rec = _Ntfy(), _Recreate()
    docker = _Docker("unhealthy")  # bleibt dauerhaft down
    lis = _make(
        ntfy=ntfy, recreate=rec, docker=docker,
        health_wait_s=30.0, health_poll_interval_s=10.0,
    )

    lis.handle_message("recreate-live", now=1000.0)
    lis.handle_message("confirm abc123", now=1010.0)

    assert "down" in ntfy.blob(-1) or "weiterhin" in ntfy.blob(-1)


def test_recreate_failure_reports_and_skips_health_poll():
    ntfy, rec = _Ntfy(ok=True), _Recreate(ok=False)
    docker = _Docker("healthy")
    lis = _make(ntfy=ntfy, recreate=rec, docker=docker)

    lis.handle_message("recreate-live", now=1000.0)
    lis.handle_message("confirm abc123", now=1010.0)

    assert rec.count == 1
    assert docker.calls == 0, "kein Health-Poll, wenn der Recreate schon scheitert"
    assert "fehl" in ntfy.blob(-1) or "fehlgeschlagen" in ntfy.blob(-1)


# ---- Subscribe-Parsing --------------------------------------------------


def test_parse_stream_line_filters_non_messages():
    assert tcl.parse_stream_line(json.dumps({"event": "open"})) is None
    assert tcl.parse_stream_line(json.dumps({"event": "keepalive"})) is None
    assert (
        tcl.parse_stream_line(
            json.dumps({"event": "message", "message": "recreate-live"})
        )
        == "recreate-live"
    )
    assert tcl.parse_stream_line("") is None
    assert tcl.parse_stream_line("   ") is None
    assert tcl.parse_stream_line("not-json{") is None
    assert tcl.parse_stream_line(json.dumps({"event": "message"})) is None


def test_unknown_message_is_ignored():
    ntfy, rec = _Ntfy(), _Recreate()
    lis = _make(ntfy=ntfy, recreate=rec)

    lis.handle_message("some-noise", now=1000.0)

    assert rec.count == 0
    assert ntfy.calls == []


# ---- main / Konfiguration ----------------------------------------------


def test_main_missing_command_topic_returns_config_exit(monkeypatch):
    monkeypatch.delenv("BG_CMD_COMMAND_TOPIC_URL", raising=False)
    monkeypatch.delenv("BG_CMD_STATUS_TOPIC_URL", raising=False)
    rc = tcl.main(["--command-topic-url", "", "--status-topic-url", ""])
    assert rc == tcl.EXIT_CONFIG


def test_main_missing_status_topic_returns_config_exit(monkeypatch):
    monkeypatch.delenv("BG_CMD_STATUS_TOPIC_URL", raising=False)
    rc = tcl.main(
        ["--command-topic-url", "https://ntfy.sh/cmd", "--status-topic-url", ""]
    )
    assert rc == tcl.EXIT_CONFIG


# ---- Subscribe-Loop: Reconnect-Robustheit -------------------------------


def test_subscribe_loop_reconnects_after_stream_error():
    """Ein Stream-Abriss (Exception) toetet den Dienst NICHT - er reconnected.

    Deckt das Kern-Risiko ab, dass ein toter Stream den always-on Listener
    lahmlegt: nach einem httpx-Fehler muss mit Backoff neu verbunden und die
    danach empfangene Nachricht regulaer verarbeitet werden.
    """
    ntfy, rec = _Ntfy(), _Recreate()
    listener = _make(ntfy=ntfy, recreate=rec)
    attempts = {"n": 0}

    def stream_lines(url: str):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("stream weg")
        yield json.dumps({"event": "message", "message": "recreate-live"})

    stop = tcl._StopFlag()
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if attempts["n"] >= 2:  # nach erfolgreichem Reconnect + Verarbeitung stoppen
            stop()

    tcl._subscribe_loop(
        listener.config,
        listener,
        stop=stop,
        sleep=fake_sleep,
        monotonic=lambda: 1000.0,
        stream_lines=stream_lines,
        initial_backoff=1.0,
        max_backoff=4.0,
    )

    assert attempts["n"] == 2, "erst Fehler, dann erfolgreicher Reconnect"
    assert sleeps[0] == 1.0, "Backoff nach dem ersten Fehler"
    # Die nach dem Reconnect empfangene 'recreate-live' wurde verarbeitet:
    assert len(ntfy.calls) == 1
    assert ntfy.calls[0][3] is not None  # Nonce-Prompt mit Bestaetigen-Action


def test_subscribe_loop_stops_on_flag_without_reading():
    """Ist das Stop-Flag schon gesetzt, wird gar nicht erst verbunden."""
    ntfy, rec = _Ntfy(), _Recreate()
    listener = _make(ntfy=ntfy, recreate=rec)
    opened = {"n": 0}

    def stream_lines(url: str):
        opened["n"] += 1
        yield from ()

    stop = tcl._StopFlag()
    stop()  # bereits gestoppt

    tcl._subscribe_loop(
        listener.config,
        listener,
        stop=stop,
        sleep=lambda _s: None,
        monotonic=lambda: 0.0,
        stream_lines=stream_lines,
    )

    assert opened["n"] == 0
