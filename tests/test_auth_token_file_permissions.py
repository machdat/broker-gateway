"""AP-10 K1: FileTokenStore prueft Permissions beim Init und schreibt
neue Dateien mit 0600.

POSIX-Tests; auf Windows skippen (NTFS-Inheritance, nicht POSIX-mode).
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from broker_gateway.auth.models import Token
from broker_gateway.auth.store import FileTokenStore


posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-Permission-Check (chmod-mode) ist auf Windows nicht aussagekraeftig",
)


def _write_token_file(path: Path, value: str = "demo-token-aaaaaaaaaaaaaaaaaa") -> None:
    """Erzeugt eine kleine valide Token-Datei (Format wie FileTokenStore es schreibt)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "tokens": [
                    {
                        "value": value,
                        "caller_id": "demo",
                        "scopes": [],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "expires_at": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


@posix_only
def test_existing_world_readable_file_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test (a): existierende 0644-Datei -> WARNING im Caplog mit chmod-0600-Hinweis."""
    target = tmp_path / "tokens.json"
    _write_token_file(target)
    os.chmod(target, 0o644)

    with caplog.at_level(logging.WARNING, logger="broker_gateway"):
        FileTokenStore(target)

    matching = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "0600" in r.getMessage()
    ]
    assert matching, (
        "Erwartete WARNING mit Hinweis auf chmod 0600 nicht gefunden. "
        f"Captured: {[r.getMessage() for r in caplog.records]}"
    )
    # Der Pfad muss in der Warnung auftauchen, damit der Operator die
    # richtige Datei findet.
    assert str(target) in matching[0].getMessage()


@posix_only
def test_existing_owner_only_file_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test (c): existierende 0600-Datei -> kein Warning."""
    target = tmp_path / "tokens.json"
    _write_token_file(target)
    os.chmod(target, 0o600)

    with caplog.at_level(logging.WARNING, logger="broker_gateway"):
        FileTokenStore(target)

    permission_warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "0600" in r.getMessage()
    ]
    assert not permission_warnings, (
        f"Unerwartete Permission-Warnung bei 0600-Datei: "
        f"{[r.getMessage() for r in permission_warnings]}"
    )


@posix_only
def test_newly_written_file_has_owner_only_permissions(tmp_path: Path) -> None:
    """Test (b): FileTokenStore schreibt neue Datei -> Permissions sind 0600."""
    target = tmp_path / "tokens.json"
    # Bewusst keinen Pre-Create der Datei - der Store legt sie selbst an,
    # sobald put() einen ersten Token persistiert.
    store = FileTokenStore(target)
    assert not target.exists(), "Setup-Annahme: Datei existiert vor put() noch nicht"

    store.put(Token(value="aaaaaaaaaaaaaaaaaaaaaaaa", caller_id="demo", scopes=[]))
    assert target.exists()

    actual_mode = stat.S_IMODE(target.stat().st_mode)
    assert actual_mode == 0o600, (
        f"Token-Datei hat mode {actual_mode:o}, erwartet 0o600"
    )


@posix_only
def test_existing_file_is_rewritten_with_0600(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Schreibvorgang nach offenem Initial-Mode reduziert die Permissions auf 0600.

    Das ist die Selbst-Heilungs-Variante: wer mit 0644-Datei startet,
    bekommt eine Warnung; spaetestens nach dem naechsten put() ist die
    Datei dauerhaft auf 0600 reduziert.
    """
    target = tmp_path / "tokens.json"
    _write_token_file(target)
    os.chmod(target, 0o644)

    with caplog.at_level(logging.WARNING, logger="broker_gateway"):
        store = FileTokenStore(target)
    store.put(Token(value="bbbbbbbbbbbbbbbbbbbbbbbb", caller_id="demo", scopes=[]))

    actual_mode = stat.S_IMODE(target.stat().st_mode)
    assert actual_mode == 0o600


def test_missing_file_does_not_warn_on_init(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Plattform-uebergreifend: nicht-existente Token-Datei -> keine Warnung.

    Die Permission-Pruefung darf nur etwas loggen, wenn es etwas zu
    pruefen gibt. Auf Windows skippt _check_permissions() ohnehin per
    Plattform-Check, dieser Test sichert das fuer beide OS.
    """
    target = tmp_path / "tokens.json"
    assert not target.exists()

    with caplog.at_level(logging.WARNING, logger="broker_gateway"):
        FileTokenStore(target)

    permission_warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "0600" in r.getMessage()
    ]
    assert not permission_warnings
