"""WebSocket-Connect-Spike gegen das lokale CP-Gateway.

THROWAWAY-SKRIPT - NICHT FUER PRODUCTION.

Ziel (KanPrompt K1, AP 23ad4490): praktisches Lernen, was beim WS-
Handshake gegen das CP-Gateway passiert. Welche unsolicited Topics
kommen direkt nach Connect, wie sieht der Auth-Erfolgsframe real aus,
wie haeufig kommen system-Heartbeats, gibt es Abweichungen zur Doku
(``docs/research/ibkr-cpapi-websockets.md``).

Voraussetzungen
---------------
- CP-Gateway laeuft auf cma-pi-1 und ist per Browser frisch eingeloggt
  (U25235077, Live). Auto-Memory ``project_ibkr_session_owner``: keine
  parallele PSM-/trading-robot-Session.
- SSH-Reverse-Tunnel offen:
  ``ssh -L 5000:localhost:5000 cma@cma-pi-1``. Hinter dem Tunnel
  spricht das CP-Gateway HTTP (nicht HTTPS) - der Tunnel ist die
  Vertraulichkeitsschicht. Default-URLs sind daher ``http://`` /
  ``ws://``. Doku-Snapshot ``ibkr-cpapi-websockets.md`` schreibt zwar
  ``wss://`` - das ist eine Abweichung des lokalen Setups und gehoert
  in die Findings.
- venv aktiv mit ``websockets>=15`` und ``httpx``. Beide sind im
  Projekt-venv vorhanden, werden aber bewusst NICHT zu pyproject.toml
  hinzugefuegt - dies ist ein Spike, kein Service-Code.

Bibliothekswahl: ``websockets`` (ASGI-/Asyncio-native, etabliert).
``httpx_ws`` waere die Alternative, bringt aber zusaetzliche Abhaengig-
keiten ohne Mehrwert fuer einen reinen Read-Loop.

Output
------
- ``tests/fixtures/recorded/ws/spike-YYYY-MM-DD.jsonl`` (eine Zeile pro
  Frame, JSON: ``{ts, dir, topic, raw, parsed}``).

Aufruf
------
    python scripts/ws_spike.py
        [--base-url https://localhost:5000]
        [--ws-url wss://localhost:5000/v1/api/ws]
        [--duration 75]
        [--ping-interval 30]
        [--out tests/fixtures/recorded/ws]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets


_DEFAULT_BASE_URL = "http://localhost:5000"
_DEFAULT_WS_URL = "ws://localhost:5000/v1/api/ws"
_DEFAULT_DURATION_S = 75
_DEFAULT_PING_INTERVAL_S = 30
_DEFAULT_OUT_DIR = Path("tests/fixtures/recorded/ws")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _insecure_ssl_context() -> ssl.SSLContext:
    """CP-Gateway nutzt lokal ein selbstsigniertes Cert - im Spike akzeptieren."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _split_frame(raw: str) -> tuple[str, Any]:
    """CP-Gateway schickt entweder reines JSON ({"topic":...}) oder das
    in der Doku beschriebene ``TOPIC+{...}``-Format. Beide tolerieren."""
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return ("?", raw)
        topic = parsed.get("topic") if isinstance(parsed, dict) else None
        return (str(topic) if topic else "?", parsed)
    if "+" in raw:
        topic, _, body = raw.partition("+")
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = body
        return (topic, parsed)
    return ("?", raw)


async def _fetch_session_id(base_url: str) -> tuple[str, httpx.Cookies]:
    async with httpx.AsyncClient(
        base_url=base_url, verify=False, timeout=10.0, trust_env=False
    ) as client:
        resp = await client.post("/v1/api/tickle")
        resp.raise_for_status()
        body = resp.json()
        session_id = body.get("session")
        if not session_id:
            raise RuntimeError(
                f"/tickle lieferte kein session-Feld: keys={list(body)}"
            )
        return session_id, client.cookies


async def _reader(
    ws: websockets.ClientConnection,
    sink: Any,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            raw_msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except websockets.ConnectionClosed:
            break
        raw = raw_msg.decode() if isinstance(raw_msg, (bytes, bytearray)) else raw_msg
        topic, parsed = _split_frame(raw)
        entry = {
            "ts": _utcnow_iso(),
            "dir": "in",
            "topic": topic,
            "raw": raw,
            "parsed": parsed,
        }
        sink.write(json.dumps(entry, ensure_ascii=False) + "\n")
        sink.flush()
        print(f"[{entry['ts']}] in  {topic:6s} {raw[:120]}", flush=True)


async def _pinger(
    ws: websockets.ClientConnection,
    sink: Any,
    interval_s: float,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await ws.send("tic")
        except websockets.ConnectionClosed:
            return
        entry = {
            "ts": _utcnow_iso(),
            "dir": "out",
            "topic": "tic",
            "raw": "tic",
            "parsed": None,
        }
        sink.write(json.dumps(entry, ensure_ascii=False) + "\n")
        sink.flush()
        print(f"[{entry['ts']}] out tic", flush=True)


async def run_spike(
    *,
    base_url: str,
    ws_url: str,
    duration_s: float,
    ping_interval_s: float,
    out_dir: Path,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"spike-{date.today().isoformat()}.jsonl"
    print(f"-> Schreibe Frames nach {out_file}")

    print(f"-> POST {base_url}/v1/api/tickle (sessionId holen)")
    session_id, cookies = await _fetch_session_id(base_url)
    print(f"-> sessionId={session_id!r}, cookies={list(cookies.jar)}")

    cookie_header = "; ".join(f"{c.name}={c.value}" for c in cookies.jar)
    extra_headers = [("Cookie", cookie_header)] if cookie_header else []

    connect_kwargs: dict[str, Any] = dict(
        additional_headers=extra_headers,
        open_timeout=10,
        ping_interval=None,
    )
    if ws_url.startswith("wss://"):
        connect_kwargs["ssl"] = _insecure_ssl_context()

    print(f"-> Connect {ws_url}")
    async with websockets.connect(ws_url, **connect_kwargs) as ws:
        with out_file.open("a", encoding="utf-8") as sink:
            connect_entry = {
                "ts": _utcnow_iso(),
                "dir": "meta",
                "topic": "connect",
                "raw": ws_url,
                "parsed": {"session_id": session_id},
            }
            sink.write(json.dumps(connect_entry, ensure_ascii=False) + "\n")
            sink.flush()

            auth_msg = json.dumps({"session": session_id})
            print(f"-> Send auth: {auth_msg}")
            await ws.send(auth_msg)
            sink.write(
                json.dumps(
                    {
                        "ts": _utcnow_iso(),
                        "dir": "out",
                        "topic": "auth",
                        "raw": auth_msg,
                        "parsed": json.loads(auth_msg),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            sink.flush()

            stop = asyncio.Event()
            reader = asyncio.create_task(_reader(ws, sink, stop))
            pinger = asyncio.create_task(
                _pinger(ws, sink, ping_interval_s, stop)
            )
            try:
                await asyncio.sleep(duration_s)
            finally:
                stop.set()
                await asyncio.gather(reader, pinger, return_exceptions=True)

    print(f"-> Spike-Run fertig: {out_file}")
    return out_file


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WS-Connect-Spike (Throwaway). Siehe Modul-Docstring."
    )
    parser.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    parser.add_argument("--ws-url", default=_DEFAULT_WS_URL)
    parser.add_argument(
        "--duration", type=float, default=_DEFAULT_DURATION_S,
        help="Aufnahme-Dauer in Sekunden (60-90 empfohlen).",
    )
    parser.add_argument(
        "--ping-interval", type=float, default=_DEFAULT_PING_INTERVAL_S,
    )
    parser.add_argument(
        "--out", type=Path, default=_DEFAULT_OUT_DIR,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        asyncio.run(
            run_spike(
                base_url=args.base_url,
                ws_url=args.ws_url,
                duration_s=args.duration,
                ping_interval_s=args.ping_interval,
                out_dir=args.out,
            )
        )
    except Exception as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
