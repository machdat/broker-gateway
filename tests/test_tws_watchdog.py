"""Tests fuer scripts/tws_watchdog.py (Karte 53c10ff4).

Die Watchdog-Logik ist ueber injizierbare Hooks (docker_inspect, http_get,
ntfy_send, recreate) und einen explizit uebergebenen ``now`` voll testbar,
ohne Docker/Netzwerk. State liegt in einer tmp-JSON-Datei.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# scripts/ ist kein Package - Pfad ergaenzen wie der Runner selbst.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import tws_watchdog as wd  # noqa: E402


_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


# ---- Test-Doubles -------------------------------------------------------


def _stacks(*, live: bool = True, paper: bool = True) -> tuple[wd.StackConfig, ...]:
    out: list[wd.StackConfig] = []
    if live:
        out.append(
            wd.StackConfig("live", "live-tws", "http://live/v1/health", is_paper=False)
        )
    if paper:
        out.append(
            wd.StackConfig("paper", "paper-tws", "http://paper/v1/health", is_paper=True)
        )
    return tuple(out)


class _Ntfy:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str, str, str | None]] = []

    def __call__(
        self, title: str, message: str, priority: str, actions: str | None = None
    ) -> bool:
        self.calls.append((title, message, priority, actions))
        return self.ok


class _Recreate:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def __call__(self, stack: str) -> bool:
        self.calls.append(stack)
        return self.ok


def _inspect(mapping: dict[str, str]):
    return lambda container: mapping.get(container, "missing")


def _http(mapping: dict[str, bool]):
    return lambda url: mapping.get(url, False)


def _run(
    stacks,
    *,
    state_file: Path,
    now: datetime = _NOW,
    inspect_map: dict[str, str],
    http_map: dict[str, bool],
    ntfy: _Ntfy,
    recreate: _Recreate,
    down_threshold: int = 2,
    realert_hours: float = 6.0,
    auto_recreate_paper: bool = True,
    command_topic_url: str = "",
):
    return wd.run(
        stacks=stacks,
        state_file=state_file,
        now=now,
        down_threshold=down_threshold,
        realert_hours=realert_hours,
        auto_recreate_paper=auto_recreate_paper,
        docker_inspect=_inspect(inspect_map),
        http_get=_http(http_map),
        ntfy_send=ntfy,
        recreate=recreate,
        command_topic_url=command_topic_url,
    )


# ---- check_stack --------------------------------------------------------


class TestCheckStack:
    def test_healthy_and_gateway_ok_is_up(self) -> None:
        cfg = _stacks(paper=False)[0]
        check = wd.check_stack(
            cfg,
            docker_inspect=_inspect({"live-tws": "healthy"}),
            http_get=_http({"http://live/v1/health": True}),
        )
        assert check.up is True

    def test_unhealthy_container_is_down(self) -> None:
        cfg = _stacks(paper=False)[0]
        check = wd.check_stack(
            cfg,
            docker_inspect=_inspect({"live-tws": "unhealthy"}),
            http_get=_http({"http://live/v1/health": True}),
        )
        assert check.up is False
        assert "unhealthy" in check.reason

    def test_gateway_unreachable_is_down(self) -> None:
        cfg = _stacks(paper=False)[0]
        check = wd.check_stack(
            cfg,
            docker_inspect=_inspect({"live-tws": "healthy"}),
            http_get=_http({"http://live/v1/health": False}),
        )
        assert check.up is False
        assert "gateway" in check.reason

    def test_starting_is_treated_as_transient_up(self) -> None:
        cfg = _stacks(paper=False)[0]
        check = wd.check_stack(
            cfg,
            docker_inspect=_inspect({"live-tws": "starting"}),
            http_get=_http({"http://live/v1/health": False}),
        )
        assert check.up is True
        assert "transient" in check.reason

    def test_missing_container_is_down(self) -> None:
        cfg = _stacks(paper=False)[0]
        check = wd.check_stack(
            cfg,
            docker_inspect=_inspect({}),  # -> "missing"
            http_get=_http({"http://live/v1/health": False}),
        )
        assert check.up is False


# ---- run / process_stack ------------------------------------------------


class TestRun:
    def test_all_up_no_action(self, tmp_path: Path) -> None:
        ntfy, recreate = _Ntfy(), _Recreate()
        outcomes = _run(
            _stacks(),
            state_file=tmp_path / "s.json",
            inspect_map={"live-tws": "healthy", "paper-tws": "healthy"},
            http_map={"http://live/v1/health": True, "http://paper/v1/health": True},
            ntfy=ntfy,
            recreate=recreate,
        )
        assert all(o.up for o in outcomes)
        assert ntfy.calls == []
        assert recreate.calls == []

    def test_down_under_threshold_no_alert(self, tmp_path: Path) -> None:
        ntfy, recreate = _Ntfy(), _Recreate()
        outcomes = _run(
            _stacks(paper=False),
            state_file=tmp_path / "s.json",
            inspect_map={"live-tws": "unhealthy"},
            http_map={"http://live/v1/health": False},
            ntfy=ntfy,
            recreate=recreate,
        )
        # 1. Lauf: consecutive=1 < threshold 2 -> kein Alarm.
        assert outcomes[0].consecutive_down == 1
        assert outcomes[0].alerted is False
        assert ntfy.calls == []

    def test_live_down_reaches_threshold_alerts_no_recreate(
        self, tmp_path: Path
    ) -> None:
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate()
        stacks = _stacks(paper=False)
        inspect_map = {"live-tws": "unhealthy"}
        http_map = {"http://live/v1/health": False}
        # 1. Lauf (unter Schwelle), 2. Lauf (Schwelle erreicht).
        _run(stacks, state_file=state, inspect_map=inspect_map, http_map=http_map,
             ntfy=ntfy, recreate=recreate)
        out2 = _run(stacks, state_file=state, inspect_map=inspect_map, http_map=http_map,
                    ntfy=ntfy, recreate=recreate)
        assert out2[0].consecutive_down == 2
        assert out2[0].alerted is True
        assert recreate.calls == []  # Live wird NIE auto-recreatet
        assert ntfy.calls[-1][2] == "urgent"  # Live-Prioritaet

    def test_paper_down_reaches_threshold_recreates_and_alerts(
        self, tmp_path: Path
    ) -> None:
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate(ok=True)
        stacks = _stacks(live=False)
        inspect_map = {"paper-tws": "unhealthy"}
        http_map = {"http://paper/v1/health": False}
        _run(stacks, state_file=state, inspect_map=inspect_map, http_map=http_map,
             ntfy=ntfy, recreate=recreate)
        out2 = _run(stacks, state_file=state, inspect_map=inspect_map, http_map=http_map,
                    ntfy=ntfy, recreate=recreate)
        assert out2[0].recreated is True
        assert recreate.calls == ["paper"]
        assert out2[0].alerted is True
        assert ntfy.calls[-1][2] == "high"  # Paper-Prioritaet

    def test_paper_recreate_only_once_per_episode(self, tmp_path: Path) -> None:
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate(ok=True)
        stacks = _stacks(live=False)
        inspect_map = {"paper-tws": "unhealthy"}
        http_map = {"http://paper/v1/health": False}
        for _ in range(4):
            _run(stacks, state_file=state, inspect_map=inspect_map, http_map=http_map,
                 ntfy=ntfy, recreate=recreate)
        # Trotz 4 Down-Laeufen nur EIN Recreate (recreated_this_episode-Latch).
        assert recreate.calls == ["paper"]

    def test_paper_recreate_failure_no_latch(self, tmp_path: Path) -> None:
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate(ok=False)  # Recreate scheitert
        stacks = _stacks(live=False)
        inspect_map = {"paper-tws": "unhealthy"}
        http_map = {"http://paper/v1/health": False}
        # threshold=1, damit beide Laeufe ueber der Schwelle liegen und der
        # Retry-nach-Fehlschlag (kein Latch) sichtbar wird.
        _run(stacks, state_file=state, inspect_map=inspect_map, http_map=http_map,
             ntfy=ntfy, recreate=recreate, down_threshold=1)
        out2 = _run(stacks, state_file=state, inspect_map=inspect_map, http_map=http_map,
                    ntfy=ntfy, recreate=recreate, down_threshold=1)
        # Gescheiterter Recreate setzt KEIN Latch -> naechster Lauf versucht erneut.
        assert recreate.calls == ["paper", "paper"]
        assert out2[0].recreated is False

    def test_no_auto_recreate_paper_flag(self, tmp_path: Path) -> None:
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate()
        stacks = _stacks(live=False)
        inspect_map = {"paper-tws": "unhealthy"}
        http_map = {"http://paper/v1/health": False}
        for _ in range(2):
            _run(stacks, state_file=state, inspect_map=inspect_map, http_map=http_map,
                 ntfy=ntfy, recreate=recreate, auto_recreate_paper=False)
        assert recreate.calls == []  # deaktiviert
        assert ntfy.calls  # aber trotzdem Alarm

    def test_realert_suppressed_within_window(self, tmp_path: Path) -> None:
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate()
        stacks = _stacks(paper=False)
        inspect_map = {"live-tws": "unhealthy"}
        http_map = {"http://live/v1/health": False}
        # Lauf 1+2 -> Alarm bei Lauf 2.
        _run(stacks, state_file=state, now=_NOW, inspect_map=inspect_map,
             http_map=http_map, ntfy=ntfy, recreate=recreate)
        _run(stacks, state_file=state, now=_NOW, inspect_map=inspect_map,
             http_map=http_map, ntfy=ntfy, recreate=recreate)
        assert len(ntfy.calls) == 1
        # Lauf 3 nur 1h spaeter -> innerhalb 6h-Fenster, kein erneuter Alarm.
        _run(stacks, state_file=state, now=_NOW + timedelta(hours=1),
             inspect_map=inspect_map, http_map=http_map, ntfy=ntfy, recreate=recreate)
        assert len(ntfy.calls) == 1

    def test_realert_after_window(self, tmp_path: Path) -> None:
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate()
        stacks = _stacks(paper=False)
        inspect_map = {"live-tws": "unhealthy"}
        http_map = {"http://live/v1/health": False}
        _run(stacks, state_file=state, now=_NOW, inspect_map=inspect_map,
             http_map=http_map, ntfy=ntfy, recreate=recreate)
        _run(stacks, state_file=state, now=_NOW, inspect_map=inspect_map,
             http_map=http_map, ntfy=ntfy, recreate=recreate)
        # 7h spaeter -> ausserhalb 6h-Fenster -> erneuter Alarm.
        _run(stacks, state_file=state, now=_NOW + timedelta(hours=7),
             inspect_map=inspect_map, http_map=http_map, ntfy=ntfy, recreate=recreate)
        assert len(ntfy.calls) == 2

    def test_recovery_sends_ok_and_resets(self, tmp_path: Path) -> None:
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate()
        stacks = _stacks(paper=False)
        down_inspect = {"live-tws": "unhealthy"}
        down_http = {"http://live/v1/health": False}
        # 2x down -> Alarm.
        _run(stacks, state_file=state, inspect_map=down_inspect, http_map=down_http,
             ntfy=ntfy, recreate=recreate)
        _run(stacks, state_file=state, inspect_map=down_inspect, http_map=down_http,
             ntfy=ntfy, recreate=recreate)
        # Jetzt wieder up -> recovered-Push.
        out = _run(stacks, state_file=state,
                   inspect_map={"live-tws": "healthy"},
                   http_map={"http://live/v1/health": True},
                   ntfy=ntfy, recreate=recreate)
        assert out[0].recovered is True
        assert out[0].consecutive_down == 0
        assert "WIEDER OK" in ntfy.calls[-1][0]

    def test_recovery_without_prior_alert_is_silent(self, tmp_path: Path) -> None:
        # Nur 1x down (unter Schwelle), dann up -> kein recovered-Push.
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate()
        stacks = _stacks(paper=False)
        _run(stacks, state_file=state, inspect_map={"live-tws": "unhealthy"},
             http_map={"http://live/v1/health": False}, ntfy=ntfy, recreate=recreate)
        out = _run(stacks, state_file=state, inspect_map={"live-tws": "healthy"},
                   http_map={"http://live/v1/health": True}, ntfy=ntfy, recreate=recreate)
        assert out[0].recovered is False
        assert ntfy.calls == []

    def test_paper_realert_text_after_recreate_latch(self, tmp_path: Path) -> None:
        # Paper recreatet (Latch), bleibt aber down -> Re-Alarm >6h spaeter
        # darf NICHT "fehlgeschlagen" sagen, sondern dass der Recreate lief.
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate(ok=True)
        stacks = _stacks(live=False)
        inspect_map = {"paper-tws": "unhealthy"}
        http_map = {"http://paper/v1/health": False}
        _run(stacks, state_file=state, now=_NOW, inspect_map=inspect_map,
             http_map=http_map, ntfy=ntfy, recreate=recreate)
        _run(stacks, state_file=state, now=_NOW, inspect_map=inspect_map,
             http_map=http_map, ntfy=ntfy, recreate=recreate)
        _run(stacks, state_file=state, now=_NOW + timedelta(hours=7),
             inspect_map=inspect_map, http_map=http_map, ntfy=ntfy, recreate=recreate)
        assert "bereits" in ntfy.calls[-1][1]
        assert recreate.calls == ["paper"]  # Latch haelt - nur 1 Recreate

    def test_recovery_push_failure_retries(self, tmp_path: Path) -> None:
        # recovered-Push schlaegt fehl -> State NICHT zuruecksetzen, naechster
        # up-Lauf versucht den Recovery-Push erneut, danach wieder still.
        state = tmp_path / "s.json"
        recreate = _Recreate()
        stacks = _stacks(paper=False)
        down_inspect = {"live-tws": "unhealthy"}
        down_http = {"http://live/v1/health": False}
        for _ in range(2):
            _run(stacks, state_file=state, inspect_map=down_inspect, http_map=down_http,
                 ntfy=_Ntfy(), recreate=recreate)
        up_inspect = {"live-tws": "healthy"}
        up_http = {"http://live/v1/health": True}
        # Recovery-Push scheitert.
        ntfy_fail = _Ntfy(ok=False)
        out1 = _run(stacks, state_file=state, inspect_map=up_inspect, http_map=up_http,
                    ntfy=ntfy_fail, recreate=recreate)
        assert out1[0].recovered is True
        assert len(ntfy_fail.calls) == 1
        # Naechster up-Lauf: Retry (State war nicht zurueckgesetzt).
        ntfy_ok = _Ntfy(ok=True)
        out2 = _run(stacks, state_file=state, inspect_map=up_inspect, http_map=up_http,
                    ntfy=ntfy_ok, recreate=recreate)
        assert out2[0].recovered is True
        assert len(ntfy_ok.calls) == 1
        # Dritter up-Lauf: State jetzt zurueckgesetzt -> still.
        ntfy_silent = _Ntfy()
        out3 = _run(stacks, state_file=state, inspect_map=up_inspect, http_map=up_http,
                    ntfy=ntfy_silent, recreate=recreate)
        assert out3[0].recovered is False
        assert ntfy_silent.calls == []


# ---- WatchdogState ------------------------------------------------------


class TestWatchdogState:
    def test_corrupt_state_file_starts_fresh(self, tmp_path: Path) -> None:
        f = tmp_path / "s.json"
        f.write_text("{ not valid json", encoding="utf-8")
        state = wd.WatchdogState.load(f)
        assert state.stacks == {}

    def test_missing_state_file_starts_fresh(self, tmp_path: Path) -> None:
        state = wd.WatchdogState.load(tmp_path / "nope.json")
        assert state.stacks == {}

    def test_save_and_reload_roundtrip(self, tmp_path: Path) -> None:
        f = tmp_path / "s.json"
        state = wd.WatchdogState()
        state.for_stack("live")["consecutive_down"] = 3
        state.save(f)
        reloaded = wd.WatchdogState.load(f)
        assert reloaded.for_stack("live")["consecutive_down"] == 3


# ---- main ---------------------------------------------------------------


class TestMain:
    def test_main_without_ntfy_url_exits_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BG_WATCHDOG_NTFY_URL", raising=False)
        assert wd.main(["--ntfy-url", ""]) == wd.EXIT_CONFIG


# ---- ops/recreate-tws.sh (Guards) ---------------------------------------


class TestRecreateScript:
    """Smoke gegen die Guard-Pfade von recreate-tws.sh (vor dem
    docker-compose-Aufruf): Live-Ablehnung + fehlendes Paper-Repo."""

    _SCRIPT = str(_REPO_ROOT / "ops" / "recreate-tws.sh")

    def _bash_or_skip(self) -> str:
        import shutil

        bash = shutil.which("bash")
        if not bash:
            pytest.skip("bash nicht verfuegbar")
        return bash

    def test_live_is_rejected(self) -> None:
        import subprocess

        bash = self._bash_or_skip()
        r = subprocess.run(
            [bash, self._SCRIPT, "live"], capture_output=True, text=True
        )
        assert r.returncode == 2
        assert "nur 'paper'" in r.stderr

    def test_missing_paper_repo_rejected(self, tmp_path: Path) -> None:
        import os
        import subprocess

        bash = self._bash_or_skip()
        env = {**os.environ, "BG_PAPER_REPO_DIR": str(tmp_path / "nope")}
        r = subprocess.run(
            [bash, self._SCRIPT, "paper"], capture_output=True, text=True, env=env
        )
        assert r.returncode == 2
        assert "nicht gefunden" in r.stderr


# ---- Live-DOWN Action-Button (Karte bd6ff0d5, AP-15) --------------------


class TestLiveDownActionButton:
    _CMD = "https://ntfy.sh/cmd-xyz"

    def _drive_live_down(
        self, state: Path, ntfy: _Ntfy, recreate: _Recreate, *, command_topic_url: str = ""
    ) -> None:
        stacks = _stacks(paper=False)
        inspect_map = {"live-tws": "unhealthy"}
        http_map = {"http://live/v1/health": False}
        for _ in range(2):  # 2 Laeufe -> Schwelle erreicht -> Alarm
            _run(
                stacks, state_file=state, inspect_map=inspect_map, http_map=http_map,
                ntfy=ntfy, recreate=recreate, command_topic_url=command_topic_url,
            )

    def test_live_down_carries_button_when_command_topic_set(
        self, tmp_path: Path
    ) -> None:
        ntfy, recreate = _Ntfy(), _Recreate()
        self._drive_live_down(
            tmp_path / "s.json", ntfy, recreate, command_topic_url=self._CMD
        )
        _t, _m, prio, actions = ntfy.calls[-1]
        assert prio == "urgent"
        assert actions is not None
        assert "recreate-live" in actions
        assert self._CMD in actions
        assert "Live-tws neu starten" in actions

    def test_live_down_no_button_without_command_topic(self, tmp_path: Path) -> None:
        ntfy, recreate = _Ntfy(), _Recreate()
        self._drive_live_down(tmp_path / "s.json", ntfy, recreate)  # default ""
        assert ntfy.calls[-1][3] is None  # kein Actions-Header
        assert "DOWN" in ntfy.calls[-1][0]  # Text unveraendert

    def test_paper_down_never_carries_button(self, tmp_path: Path) -> None:
        ntfy, recreate = _Ntfy(), _Recreate(ok=True)
        stacks = _stacks(live=False)
        inspect_map = {"paper-tws": "unhealthy"}
        http_map = {"http://paper/v1/health": False}
        for _ in range(2):
            _run(
                stacks, state_file=tmp_path / "s.json", inspect_map=inspect_map,
                http_map=http_map, ntfy=ntfy, recreate=recreate,
                command_topic_url=self._CMD,
            )
        # Auch bei gesetztem Command-Topic traegt der Paper-Alarm keinen Button.
        assert ntfy.calls[-1][3] is None

    def test_recovery_push_never_carries_button(self, tmp_path: Path) -> None:
        state = tmp_path / "s.json"
        ntfy, recreate = _Ntfy(), _Recreate()
        self._drive_live_down(state, ntfy, recreate, command_topic_url=self._CMD)
        stacks = _stacks(paper=False)
        _run(
            stacks, state_file=state, inspect_map={"live-tws": "healthy"},
            http_map={"http://live/v1/health": True}, ntfy=ntfy, recreate=recreate,
            command_topic_url=self._CMD,
        )
        assert "WIEDER OK" in ntfy.calls[-1][0]
        assert ntfy.calls[-1][3] is None  # Recovery-Push ohne Button
