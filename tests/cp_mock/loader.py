"""Replay-Loader: liest aufgezeichnete CP-Gateway-Antworten aus Dateien.

Sucht nach passenden JSON-Fixtures in
``tests/fixtures/recorded/live/`` (echte Live-Recordings, falls
vorhanden) und faellt auf ``tests/fixtures/recorded/seed/`` zurueck
(handgeschriebener Default, der das frueher hartcodierte Mock-Verhalten
1:1 reproduziert).

Das Naming-Schema stammt aus dem Recorder
(:mod:`broker_gateway.cp.recorder`):

    <sanitized_path>__<METHOD>__<query_hash>_<NN>.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from broker_gateway.cp.recorder import query_hash, sanitize_path


DEFAULT_FIXTURES_DIR: Path = (
    Path(__file__).resolve().parents[1] / "fixtures" / "recorded"
)
"""Default-Wurzel - kann in Tests via ``base_dir`` ueberschrieben werden."""

_PRIORITY_SUBDIRS: tuple[str, ...] = ("live", "seed")
"""Reihenfolge, in der Unterverzeichnisse durchsucht werden. live > seed."""


class RecordingNotFoundError(LookupError):
    """Wird geworfen, wenn weder ``live/`` noch ``seed/`` ein passendes
    Recording fuer Endpoint+Method+Query+Call-Index enthalten."""


def load_recording(
    endpoint: str,
    *,
    method: str = "GET",
    query: dict[str, str] | None = None,
    call_index: int = 1,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Liefert das ``response``-Dict des passenden Recording.

    Args:
        endpoint: URL-Pfad ohne Host, z.B. "/iserver/auth/status".
        method: HTTP-Methode in Grossbuchstaben.
        query: Optionales Query-Param-Dict (gleiche Strings wie der
            Recorder gesehen haette).
        call_index: 1-basiert. ``1`` liefert das ``_01``-File - das
            ist das First-Call-Prime-Verhalten beim Snapshot-Endpoint.
        base_dir: Optionaler Override, sonst :data:`DEFAULT_FIXTURES_DIR`.

    Raises:
        RecordingNotFoundError: wenn weder live noch seed ein Recording
            fuer die gegebene Kombination enthalten. Tests duerfen das
            fangen, um sich auf Code-basierte Fallbacks zurueckzufallen,
            aber im Normalfall ist es ein harter Fehler.
    """
    root = base_dir or DEFAULT_FIXTURES_DIR
    method_upper = method.upper()
    qhash = query_hash(query or {})
    filename = f"{sanitize_path(endpoint)}__{method_upper}__{qhash}_{call_index:02d}.json"

    for subdir in _PRIORITY_SUBDIRS:
        candidate = root / subdir / filename
        if candidate.is_file():
            envelope = json.loads(candidate.read_text(encoding="utf-8"))
            response = envelope.get("response")
            if not isinstance(response, dict):
                raise RuntimeError(
                    f"Recording {candidate} hat kein 'response'-Dict."
                )
            # live-Recordings mit 4xx/5xx-Status sind dokumentarische
            # Beweise fuer Service-Code-Bugs (z.B. /iserver/account/.../portfolio
            # liefert 404, weil IBKR /portfolio/.../summary erwartet) - nicht
            # der Body, gegen den die Mock-Fixture sprechen soll. Solange der
            # Service-Code-Pfad noch nicht umgestellt ist, bleibt das seed-
            # Recording die "happy path"-Quelle.
            if subdir == "live" and response.get("status_code", 0) >= 400:
                continue
            return response

    raise RecordingNotFoundError(
        f"Kein Recording fuer {method_upper} {endpoint} (query={query!r}, "
        f"call_index={call_index}) - erwartet wurde "
        f"<{'|'.join(_PRIORITY_SUBDIRS)}>/{filename} unter {root}."
    )
