"""Token-Store - In-Memory-Default + optionaler File-Backend.

`TokenStore` ist als Protocol definiert, damit spätere Backends (Redis,
SQL, ...) ohne Refactoring im Aufrufer eingehängt werden können.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Iterable, Protocol

from broker_gateway.auth.models import Token, deserialize_token, serialize_token


_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) ergibt ~43 Zeichen


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

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        for entry in payload.get("tokens", []):
            token = deserialize_token(entry)
            self._tokens[token.value] = token

    def _persist_locked(self) -> None:
        payload = {"tokens": [serialize_token(t) for t in self._tokens.values()]}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
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
