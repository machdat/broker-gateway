"""Recorder fuer CP-Gateway-Live-Verkehr.

Schreibt jeden HTTP-Roundtrip eines httpx.AsyncClient als deterministische
JSON-Fixture. Aktivierung ausschliesslich ueber ENV ``BG_CP_RECORD_DIR``
oder explizite Instanziierung - im Produktiv-Default ist der Recorder
nicht aktiv und verursacht keine Disk-IO.

Geheimnisse (Authorization, Cookie, Set-Cookie, X-API-Key) werden
clientseitig **vor dem Schreiben** gefiltert. Body-Inhalte werden ueber
:func:`broker_gateway.cp.normalize.normalize_response` von nicht-
deterministischen Feldern (Timestamps, Order-IDs, Session-IDs) befreit,
damit aufgezeichnete Fixtures byte-identisch bleiben.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from broker_gateway.cp.normalize import normalize_response
from broker_gateway.cp.redaction import REDACTED_HEADERS, filter_headers


_LOG = logging.getLogger(__name__)


class CPRecorder:
    """Persistiert HTTP-Verkehr eines httpx.AsyncClient als JSON-Fixtures."""

    def __init__(
        self,
        record_dir: Path | str,
        *,
        normalize_prices: bool = False,
        base_path: str | None = None,
    ) -> None:
        self.record_dir = Path(record_dir)
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.normalize_prices = normalize_prices
        # Pfad-Prefix, der beim Schreiben abgeschnitten wird. None = beim
        # ersten install_into() automatisch aus client.base_url uebernehmen.
        self.base_path: str | None = (
            base_path.rstrip("/") if base_path is not None else None
        )
        self._call_counter: dict[tuple[str, str, str], int] = {}
        self._active: bool = True

    def __enter__(self) -> "CPRecorder":
        self._active = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._active = False

    def install_into(self, client: httpx.AsyncClient) -> None:
        if self.base_path is None:
            client_path = (client.base_url.path or "").rstrip("/")
            self.base_path = client_path
        request_hooks = list(client.event_hooks.get("request", []))
        response_hooks = list(client.event_hooks.get("response", []))
        response_hooks.append(self._on_response)
        client.event_hooks = {"request": request_hooks, "response": response_hooks}

    async def _on_response(self, response: httpx.Response) -> None:
        if not self._active:
            return
        request = response.request
        await response.aread()
        try:
            self._write(request, response)
        except Exception as exc:  # noqa: BLE001
            # Recorder darf nie das Live-Verhalten brechen - nur warnen.
            _LOG.warning(
                "CPRecorder konnte %s %s nicht persistieren: %s",
                request.method, request.url.path, exc,
            )

    def _write(self, request: httpx.Request, response: httpx.Response) -> None:
        path = request.url.path
        if self.base_path and path.startswith(self.base_path):
            stripped = path[len(self.base_path):]
            path = stripped if stripped.startswith("/") else "/" + stripped
        method = request.method.upper()
        query = _query_dict(request.url)
        qhash = _query_hash(query)
        target = self._next_target(path, method, qhash)

        req_json, req_text = _split_body(
            request.content, request.headers.get("content-type")
        )
        resp_json, resp_text = _split_body(
            response.content, response.headers.get("content-type")
        )
        if resp_json is not None:
            resp_json = normalize_response(
                resp_json, path, normalize_prices=self.normalize_prices
            )
        if req_json is not None:
            req_json = normalize_response(
                req_json, path, normalize_prices=self.normalize_prices
            )

        envelope = {
            "request": {
                "method": method,
                "url": path,
                "query": query,
                "headers": filter_headers(request.headers),
                "body_json": req_json,
                "body_text": req_text,
            },
            "response": {
                "status_code": response.status_code,
                "headers": filter_headers(response.headers),
                "body_json": resp_json,
                "body_text": resp_text,
            },
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "normalized": True,
        }
        target.write_text(
            json.dumps(envelope, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _LOG.info(
            "CPRecorder %s %s -> %s (HTTP %s)",
            method, path, target.name, response.status_code,
        )

    def _next_target(self, path: str, method: str, qhash: str) -> Path:
        key = (path, method, qhash)
        n = self._call_counter.get(key, 0) + 1
        self._call_counter[key] = n
        sanitized = _sanitize_path(path)
        return self.record_dir / f"{sanitized}__{method}__{qhash}_{n:02d}.json"


def sanitize_path(path: str) -> str:
    """Wandelt einen URL-Pfad in einen dateinamen-tauglichen Slug.

    Public, weil der Replay-Loader (tests/cp_mock/loader.py) dieselbe
    Konvention braucht, um aufgezeichnete Dateien wiederzufinden.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", path.strip("/"))
    return cleaned or "root"


def query_hash(query: dict[str, str]) -> str:
    """Stabiler 8-Hex-Slug ueber sortierte Query-Parameter.

    Public aus demselben Grund wie :func:`sanitize_path`.
    """
    if not query:
        return "noquery"
    sorted_query = {k: query[k] for k in sorted(query.keys())}
    serialized = "&".join(f"{k}={v}" for k, v in sorted_query.items())
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:8]


def _query_dict(url: httpx.URL) -> dict[str, str]:
    return {key: url.params[key] for key in sorted(url.params.keys())}


# Backward-kompatible Aliase fuer recorder-internen Code.
_sanitize_path = sanitize_path
_query_hash = query_hash


def _split_body(content: bytes, content_type: str | None) -> tuple[Any | None, str | None]:
    if not content:
        return None, None
    ct_lower = (content_type or "").lower()
    looks_like_json = "json" in ct_lower or content[:1] in (b"{", b"[")
    if looks_like_json:
        try:
            return json.loads(content.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    try:
        return None, content.decode("utf-8")
    except UnicodeDecodeError:
        return None, None
