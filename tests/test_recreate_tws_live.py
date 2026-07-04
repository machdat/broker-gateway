"""Tests fuer ops/recreate-tws-live.sh (Karte c3839836, AP-15).

Das Skript ist ein abgesicherter One-Shot Live-tws-force-recreate. Getestet
wird es als Subprozess mit einem ``docker``-Shim im PATH, der jeden Aufruf in
eine Log-Datei schreibt (kein echtes Docker noetig). So sind Guard-Verhalten,
Pre-Recreate-Log-Capture und die Ein-Recreate-Garantie ohne Container
verifizierbar - auf ubuntu-CI wie lokal via Git Bash.

Abweichung vom Karten-Wortlaut (bewusst, abwaertskompatibel): das Ziel-
Verzeichnis der Crash-Logs ist ueber BG_CRASH_LOG_DIR ueberschreibbar,
Default bleibt /tmp. Ohne diese Stellschraube liesse sich der Happy-Path
nicht plattformunabhaengig pruefen.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIVE_SCRIPT = _REPO_ROOT / "ops" / "recreate-tws-live.sh"
_PAPER_SCRIPT = _REPO_ROOT / "ops" / "recreate-tws.sh"

# Absoluten bash-Pfad festhalten: sonst trifft CreateProcess unter Windows
# den WSL-Stub in System32 statt Git Bash. Auf ubuntu-CI ist das /usr/bin/bash.
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(_BASH is None, reason="bash nicht verfuegbar")


def _posix(path: Path) -> str:
    """Windows-Pfad -> MSYS/POSIX-Form, damit bash ihn im PATH/cd versteht.

    Auf Linux (CI) ist der Pfad bereits POSIX und bleibt unveraendert.
    """
    s = str(path).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(.*)$", s)
    if m:
        return f"/{m.group(1).lower()}{m.group(2)}"
    return s


def _make_docker_shim(shim_dir: Path, *, logs_fail: bool = False) -> None:
    """Legt ein ausfuehrbares 'docker' an, das jeden Aufruf protokolliert."""
    shim = shim_dir / "docker"
    logs_exit = "1" if logs_fail else "0"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$DOCKER_CALL_LOG"\n'
        'if [ "$1" = "logs" ]; then\n'
        '  echo "fake container log line"\n'
        f"  exit {logs_exit}\n"
        "fi\n"
        "exit 0\n"
    )
    shim.chmod(0o755)


def _make_live_repo(base: Path, *, with_env: bool = True) -> Path:
    repo = base / "live-repo"
    repo.mkdir()
    (repo / "compose.yaml").write_text("services: {}\n")
    if with_env:
        (repo / ".env").write_text("BG_STACK_KIND=live\n")
    return repo


def _run_live(
    tmp_path: Path,
    *,
    allow: bool,
    repo: Path | None,
    crash_dir: Path,
    shim_dir: Path,
    call_log: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Ambiente BG_*-Vars der Dev-Shell duerfen den Test nicht verfaelschen:
    # BG_ENV_FILE wird bewusst entfernt, damit der Skript-Default '.env' greift
    # (die anderen relevanten Vars werden unten explizit gesetzt).
    env.pop("BG_ENV_FILE", None)
    env["DOCKER_CALL_LOG"] = _posix(call_log)
    env["BG_CRASH_LOG_DIR"] = _posix(crash_dir)
    if allow:
        env["BG_ALLOW_LIVE_RECREATE"] = "yes"
    else:
        env.pop("BG_ALLOW_LIVE_RECREATE", None)
    if repo is not None:
        env["BG_LIVE_REPO_DIR"] = _posix(repo)
    else:
        env["BG_LIVE_REPO_DIR"] = _posix(tmp_path / "does-not-exist")
    inner = f'export PATH="{_posix(shim_dir)}:$PATH"; exec bash "{_posix(_LIVE_SCRIPT)}"'
    return subprocess.run(
        [_BASH, "-c", inner],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def rig(tmp_path: Path):
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    crash_dir = tmp_path / "crash"
    crash_dir.mkdir()
    call_log = tmp_path / "docker-calls.log"
    _make_docker_shim(shim_dir)
    return {
        "tmp_path": tmp_path,
        "shim_dir": shim_dir,
        "crash_dir": crash_dir,
        "call_log": call_log,
    }


# ---- Guard: Opt-in Pflicht -------------------------------------------------


def test_refuses_without_opt_in(rig):
    repo = _make_live_repo(rig["tmp_path"])
    r = _run_live(
        rig["tmp_path"], allow=False, repo=repo,
        crash_dir=rig["crash_dir"], shim_dir=rig["shim_dir"], call_log=rig["call_log"],
    )
    assert r.returncode == 2, r.stderr
    assert not rig["call_log"].exists(), "keine docker-Aktion ohne Opt-in erlaubt"
    assert "BG_ALLOW_LIVE_RECREATE" in r.stderr


def test_refuses_when_repo_missing(rig):
    r = _run_live(
        rig["tmp_path"], allow=True, repo=None,
        crash_dir=rig["crash_dir"], shim_dir=rig["shim_dir"], call_log=rig["call_log"],
    )
    assert r.returncode == 2, r.stderr
    assert not rig["call_log"].exists(), "kein docker-Call bei fehlendem Live-Repo"


def test_refuses_when_env_file_missing(rig):
    repo = _make_live_repo(rig["tmp_path"], with_env=False)
    r = _run_live(
        rig["tmp_path"], allow=True, repo=repo,
        crash_dir=rig["crash_dir"], shim_dir=rig["shim_dir"], call_log=rig["call_log"],
    )
    assert r.returncode == 2, r.stderr
    assert not rig["call_log"].exists(), "kein docker-Call bei fehlender .env"


# ---- Happy-Path: Log-Capture, dann genau EIN Recreate ----------------------


def test_happy_path_captures_logs_then_single_recreate(rig):
    repo = _make_live_repo(rig["tmp_path"])
    r = _run_live(
        rig["tmp_path"], allow=True, repo=repo,
        crash_dir=rig["crash_dir"], shim_dir=rig["shim_dir"], call_log=rig["call_log"],
    )
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"

    lines = rig["call_log"].read_text().splitlines()
    # Genau zwei docker-Aufrufe: erst logs (Forensik), dann compose up.
    assert len(lines) == 2, lines
    assert lines[0].startswith("logs"), lines
    assert "broker-gateway-tws" in lines[0], lines
    assert "compose" in lines[1] and "up -d --force-recreate tws" in lines[1], lines
    assert "--env-file .env" in lines[1] and "-f compose.yaml" in lines[1], lines
    # Ein-Recreate-Garantie: kein Loop.
    assert sum("--force-recreate" in ln for ln in lines) == 1, lines

    # Reihenfolge: Forensik VOR dem Recreate.
    assert lines.index([ln for ln in lines if ln.startswith("logs")][0]) == 0

    # Crash-Log wurde geschrieben und der Pfad auf stdout gemeldet.
    crash_files = list(rig["crash_dir"].glob("tws-crash-*.log"))
    assert len(crash_files) == 1, crash_files
    assert "fake container log line" in crash_files[0].read_text()
    assert "tws-crash-" in r.stdout


def test_forensik_capture_is_best_effort(rig):
    # docker logs schlaegt fehl -> der Recreate muss trotzdem laufen.
    _make_docker_shim(rig["shim_dir"], logs_fail=True)
    repo = _make_live_repo(rig["tmp_path"])
    r = _run_live(
        rig["tmp_path"], allow=True, repo=repo,
        crash_dir=rig["crash_dir"], shim_dir=rig["shim_dir"], call_log=rig["call_log"],
    )
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"
    lines = rig["call_log"].read_text().splitlines()
    assert sum("--force-recreate" in ln for ln in lines) == 1, lines


# ---- Regression: recreate-tws.sh Paper-Default unveraendert ----------------


def test_paper_script_rejects_missing_arg():
    r = subprocess.run(
        [_BASH, "-c", f'exec bash "{_posix(_PAPER_SCRIPT)}"'],
        env=os.environ.copy(), capture_output=True, text=True,
    )
    assert r.returncode == 2, r.stderr


def test_paper_script_rejects_live_arg():
    r = subprocess.run(
        [_BASH, "-c", f'exec bash "{_posix(_PAPER_SCRIPT)}" live'],
        env=os.environ.copy(), capture_output=True, text=True,
    )
    assert r.returncode == 2, r.stderr
