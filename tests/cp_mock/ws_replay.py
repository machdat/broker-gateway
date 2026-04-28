"""WS-Recording-Replay: deterministische In-Memory-Iteration ueber JSONL-Frames.

Strikt getrennt von der REST-Replay-Infrastruktur (``loader.py`` /
``replay.py``). Frames leben unter ``tests/fixtures/recorded/ws/`` und
folgen dem in ``docs/research/ibkr-cpapi-websockets-findings.md``
dokumentierten Schema.

Schema pro Zeile (JSON-Object):

    {
      "ts":     "<ISO-8601 mit ms+TZ>",   # Pflicht
      "dir":    "in" | "out" | "meta",    # Pflicht
      "topic":  "<3-buchstaben-topic | system | sts | act | tic | ...>",
      "raw":    "<rohstring vom Wire>",   # Pflicht (kann leer sein)
      "parsed": <object | null>,          # optional, bereits gesplitteter Body
      "session_id": "<...>"               # optional, Schema-erweiterung
    }

Der Replay arbeitet auf JSONL-Ebene und oeffnet **kein** WS-Server-
Socket. Zwei Modi:

- :func:`iter_server_frames` - liefert nur ``dir=="in"``-Frames in
  Reihenfolge. Tests fuer einen Client koennen damit das Server-
  Verhalten nachstellen, ohne TCP zu sprechen.
- :func:`iter_client_frames` - liefert nur ``dir=="out"``-Frames.
  Tests gegen einen Server-Stub koennen damit pruefen, was der
  Echt-Client beim Live-Lauf gesendet hat.

Inter-Frame-Delay
-----------------

Per Default werden die Frames synchron ohne Wartezeit ausgegeben - das
ist der typische Test-Pfad (Reihenfolge + Bodies pruefen). Mit
``timing="real"`` sleept der Generator zwischen zwei Frames ihre
echte Differenz aus den Timestamps; ``timing="compressed"`` skaliert
das mit ``compression_factor``. ``"real"`` ist nur fuer Soak-Tests
gedacht, nicht fuer die Standard-pytest-Suite.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


_REQUIRED_KEYS: tuple[str, ...] = ("ts", "dir", "topic", "raw")
_VALID_DIRS: frozenset[str] = frozenset({"in", "out", "meta"})


class WSReplayError(ValueError):
    """Schema- oder Datei-Fehler im WS-Recording."""


@dataclass(frozen=True)
class WSFrame:
    """Ein einzelner Frame im WS-Recording.

    ``parsed`` ist der bereits dekodierte Body falls verfuegbar, sonst
    ``None``. ``raw`` ist immer der Wire-String (z.B. ``"tic"`` oder
    ``'{"topic":"system",...}'``). Aenderungen am Schema muessen
    abwaerts lesbar bleiben - neue Felder kommen in ``extras``.
    """

    ts: datetime
    dir: Literal["in", "out", "meta"]
    topic: str
    raw: str
    parsed: Any | None = None
    extras: dict[str, Any] | None = None

    @property
    def body(self) -> Any:
        """Liefert ``parsed`` falls vorhanden, sonst ``raw``.

        Praktischer Helper fuer Tests, die nicht zwischen
        JSON-Frames (``parsed`` gesetzt) und Plain-String-Frames
        (z.B. ``"tic"``) unterscheiden wollen.
        """
        return self.parsed if self.parsed is not None else self.raw


def _parse_ts(value: object, lineno: int) -> datetime:
    if not isinstance(value, str):
        raise WSReplayError(
            f"Zeile {lineno}: 'ts' muss String sein, bekommen {type(value).__name__}"
        )
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise WSReplayError(
            f"Zeile {lineno}: 'ts'={value!r} ist kein ISO-Timestamp ({exc})"
        ) from exc


def _validate_frame_dict(obj: Any, lineno: int) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise WSReplayError(
            f"Zeile {lineno}: erwarte JSON-Object, bekommen {type(obj).__name__}"
        )
    for key in _REQUIRED_KEYS:
        if key not in obj:
            raise WSReplayError(
                f"Zeile {lineno}: Pflichtfeld {key!r} fehlt"
            )
    if obj["dir"] not in _VALID_DIRS:
        raise WSReplayError(
            f"Zeile {lineno}: 'dir'={obj['dir']!r} - erlaubt {sorted(_VALID_DIRS)}"
        )
    if not isinstance(obj["topic"], str):
        raise WSReplayError(
            f"Zeile {lineno}: 'topic' muss String sein"
        )
    if not isinstance(obj["raw"], str):
        raise WSReplayError(
            f"Zeile {lineno}: 'raw' muss String sein (auch bei tic ohne Body)"
        )
    return obj


def load_ws_frames(path: str | Path) -> list[WSFrame]:
    """Liest eine WS-Recording-JSONL-Datei und gibt eine Liste von
    :class:`WSFrame` in Datei-Reihenfolge zurueck.

    Validiert Schema strikt - eine Zeile mit fehlendem Pflichtfeld
    fuehrt zu :class:`WSReplayError` mit Zeilennummer. Leere Zeilen
    werden uebersprungen.

    Schema-Backwards-Compat: zusaetzliche Felder werden in
    ``WSFrame.extras`` durchgereicht; aeltere Tests bleiben gruen.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise WSReplayError(f"WS-Recording nicht gefunden: {file_path}")

    frames: list[WSFrame] = []
    with file_path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise WSReplayError(
                    f"Zeile {lineno}: kein gueltiges JSON ({exc.msg})"
                ) from exc
            data = _validate_frame_dict(obj, lineno)
            extras = {
                k: v for k, v in data.items()
                if k not in {*_REQUIRED_KEYS, "parsed"}
            }
            frames.append(
                WSFrame(
                    ts=_parse_ts(data["ts"], lineno),
                    dir=data["dir"],
                    topic=data["topic"],
                    raw=data["raw"],
                    parsed=data.get("parsed"),
                    extras=extras or None,
                )
            )
    return frames


def _iter_with_delay(
    frames: Iterable[WSFrame],
    *,
    timing: Literal["none", "real", "compressed"],
    compression_factor: float,
    sleep: Any,
) -> Iterator[WSFrame]:
    prev_ts: datetime | None = None
    for frame in frames:
        if timing != "none" and prev_ts is not None:
            delta_s = (frame.ts - prev_ts).total_seconds()
            if delta_s > 0:
                if timing == "compressed":
                    delta_s = delta_s * compression_factor
                sleep(delta_s)
        prev_ts = frame.ts
        yield frame


def iter_server_frames(
    frames: Iterable[WSFrame],
    *,
    timing: Literal["none", "real", "compressed"] = "none",
    compression_factor: float = 0.0,
    sleep: Any = time.sleep,
) -> Iterator[WSFrame]:
    """Generator ueber alle ``dir=="in"``-Frames (Server -> Client).

    Test-Code ruft das auf, wenn er die Server-Seite eines WS-
    Dialogs replayen will - genau die Frames, die der Echt-Server
    gepusht hat.
    """
    server_frames = (f for f in frames if f.dir == "in")
    yield from _iter_with_delay(
        server_frames,
        timing=timing,
        compression_factor=compression_factor,
        sleep=sleep,
    )


def iter_client_frames(
    frames: Iterable[WSFrame],
    *,
    timing: Literal["none", "real", "compressed"] = "none",
    compression_factor: float = 0.0,
    sleep: Any = time.sleep,
) -> Iterator[WSFrame]:
    """Generator ueber alle ``dir=="out"``-Frames (Client -> Server).

    Test-Code, der einen Server-Stub testet, kann damit
    pruefen, dass die richtigen Subscribe-/Auth-/tic-Frames
    rausgehen wuerden.
    """
    client_frames = (f for f in frames if f.dir == "out")
    yield from _iter_with_delay(
        client_frames,
        timing=timing,
        compression_factor=compression_factor,
        sleep=sleep,
    )
