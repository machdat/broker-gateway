"""Token-Store - In-Memory-Default + optionaler File-Backend.

`TokenStore` ist als Protocol definiert, damit spätere Backends (Redis,
SQL, ...) ohne Refactoring im Aufrufer eingehängt werden können.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import sys
import threading
from pathlib import Path
from typing import Iterable, Protocol

from broker_gateway.auth.models import Token, deserialize_token, serialize_token


_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) ergibt ~43 Zeichen
_TOKEN_FILE_MODE = 0o600
# Bits, deren Vorhandensein eine Warning ausloest: jeder Lese-/Schreib-/
# Ausfuehrungs-Zugriff fuer group oder other ist fuer einen Token-Store
# zu offen.
_OPEN_PERMISSION_MASK = (
    stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP
    | stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH
)

_log = logging.getLogger("broker_gateway")


def generate_token_value() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


class TokenStore(Protocol):
    """Persistierungs-Schicht für Tokens.

    Implementierungen müssen thread-safe sein - die FastAPI-App wird
    aus mehreren Workern bzw. asynchron aus dem Event-Loop heraus auf
    den Store zugreifen.
    """

    def get(self, value: str) -> Token | None: ...

    def put(self, token: Token) -> None: ...

    def delete(self, value: str) -> bool: ...

    def list(self) -> list[Token]: ...


class InMemoryTokenStore:
    """Default-Implementierung. Verliert State beim Neustart - das ist Absicht
    (Service ist transient, Anhang Abschnitt 3.4).
    """

    def __init__(self, initial: Iterable[Token] = ()) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, Token] = {}
        for token in initial:
            self._tokens[token.value] = token

    def get(self, value: str) -> Token | None:
        with self._lock:
            return self._tokens.get(value)

    def put(self, token: Token) -> None:
        with self._lock:
            self._tokens[token.value] = token

    def delete(self, value: str) -> bool:
        with self._lock:
            return self._tokens.pop(value, None) is not None

    def list(self) -> list[Token]:
        with self._lock:
            return list(self._tokens.values())


class FileTokenStore:
    """Persistiert Tokens in einer JSON-Datei.

    Aktiviert über ENV `BG_TOKEN_FILE`. Pfad wird beim Schreiben atomar
    via temp-file + rename ersetzt, damit ein abgebrochener Schreibvorgang
    keinen halben State hinterlässt.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._lock = threading.Lock()
        self._path = Path(path)
        self._tokens: dict[str, Token] = {}
        self._load()
        self._check_permissions()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        for entry in payload.get("tokens", []):
            token = deserialize_token(entry)
            self._tokens[token.value] = token

    def _check_permissions(self) -> None:
        """Logt eine Warnung, wenn die Token-Datei zu offene Permissions hat.

        Auf POSIX wird der ``stat.st_mode`` gegen group-/other-Zugriff
        geprueft (``_OPEN_PERMISSION_MASK``). Auf Windows greift NTFS-
        Inheritance, ein POSIX-mode-Check liefert dort kein verlaessliches
        Ergebnis - daher wird die Pruefung mit einem Debug-Log
        uebersprungen.

        Kein Crash bei zu offenen Rechten: der Service muss auch in
        Setups starten, in denen der Operator bewusst Group-Read
        zulaesst (z.B. fuer ein Backup-Tool). Die Warnung dokumentiert
        die Lage im app.log-Strang.
        """
        if sys.platform == "win32":
            _log.debug(
                "FileTokenStore Permission-Check auf Windows uebersprungen (NTFS, kein POSIX-mode)",
                extra={"path": str(self._path)},
            )
            return
        if not self._path.exists():
            return
        try:
            mode = self._path.stat().st_mode
        except OSError as exc:
            _log.warning(
                "FileTokenStore: stat() auf Token-Datei fehlgeschlagen: %s",
                exc,
                extra={"path": str(self._path)},
            )
            return
        if mode & _OPEN_PERMISSION_MASK:
            _log.warning(
                "FileTokenStore: Token-Datei %s hat zu offene Permissions (mode=%o). "
                "Empfehlung: chmod 0600 %s",
                self._path,
                stat.S_IMODE(mode),
                self._path,
                extra={"path": str(self._path), "mode": stat.S_IMODE(mode)},
            )

    def _persist_locked(self) -> None:
        payload = {"tokens": [serialize_token(t) for t in self._tokens.values()]}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        # Vor dem atomaren Rename die restriktiven Permissions setzen,
        # damit das Ziel-File nie mit Default-Umask im Filesystem auftaucht.
        # Auf Windows ist chmod ein No-Op fuer POSIX-Bits (NTFS regelt
        # das ueber ACLs); wir lassen den Aufruf trotzdem stehen - das
        # ist dort einfach wirkungslos und konsistent zum bestehenden
        # Pattern ohne Plattform-Sonderfall.
        try:
            os.chmod(tmp, _TOKEN_FILE_MODE)
        except OSError as exc:
            # Best-effort: wenn chmod scheitert (z.B. exotisches FS),
            # nur eine Warnung loggen, nicht crashen. Atomares Rename
            # findet trotzdem statt.
            _log.warning(
                "FileTokenStore: chmod 0600 auf %s fehlgeschlagen: %s",
                tmp, exc,
                extra={"path": str(tmp)},
            )
        os.replace(tmp, self._path)

    def get(self, value: str) -> Token | None:
        with self._lock:
            return self._tokens.get(value)

    def put(self, token: Token) -> None:
        with self._lock:
            self._tokens[token.value] = token
            self._persist_locked()

    def delete(self, value: str) -> bool:
        with self._lock:
            existed = self._tokens.pop(value, None) is not None
            if existed:
                self._persist_locked()
            return existed

    def list(self) -> list[Token]:
        with self._lock:
            return list(self._tokens.values())


def build_default_store() -> TokenStore:
    """Wählt Backend abhängig von ENV `BG_TOKEN_FILE`."""
    file_path = os.environ.get("BG_TOKEN_FILE")
    if file_path:
        return FileTokenStore(file_path)
    return InMemoryTokenStore()
