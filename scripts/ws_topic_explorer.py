"""IBKR CP-Gateway WebSocket Topic Explorer - K4 (AP-04).

Systematische Live-Erkundung der drei wichtigsten Topics ``smd``
(Marktdaten), ``sor`` (Order-Updates) und ``str`` (Trades) gegen die
laufende U25235077-Session. Begleit-Doku der Findings in
``docs/research/ibkr-cpapi-websockets-findings.md``, Mitschnitte
landen unter ``tests/fixtures/recorded/ws/topic-explorer-YYYY-MM-DD/``.

Setup-Voraussetzungen
---------------------
- CP-Gateway laeuft auf cma-pi-1 mit aktiver U25235077-Session (Browser-
  Login per Runbook ``docs/runbooks/cpgateway-login.md``).
- Auf der Pi ist Port 5000 lokal erreichbar - entweder per Login-Override
  (``compose.login-override.yaml``) oder das Skript laeuft im Compose-
  Netzwerk und nutzt ``--base-url http://cpgateway:5000``.
- Auto-Memory ``project_ibkr_session_owner`` beachten: keine parallele
  PSM-/trading-robot-Session.

Bekannte Einschraenkung
-----------------------
``CPWebSocketClient`` konsumiert die initialen ``system+success`` /
``act`` / ``sts``-Frames intern (Auth-Wait). Im Mitschnitt fehlt damit
das Connect-Burst, das K1 bereits ausgiebig dokumentiert hat. Fuer die
Topic-Exploration ist das ohne Belang - smd/sor/str-Frames kommen alle
nach Subscribe, also nach Auth-Ack.

Aufruf-Beispiele
----------------
    # Einzelnes Szenario:
    python scripts/ws_topic_explorer.py smd-single
    python scripts/ws_topic_explorer.py smd-multi
    python scripts/ws_topic_explorer.py smd-large
    python scripts/ws_topic_explorer.py str-trades
    python scripts/ws_topic_explorer.py reconnect

    # sor (mit Live-Test-Order):
    python scripts/ws_topic_explorer.py sor --with-test-order

    # Alle ohne sor (Default):
    python scripts/ws_topic_explorer.py all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from broker_gateway.cp.ws_client import CPWebSocketClient


_DEFAULT_BASE_URL = "http://localhost:5000"
_DEFAULT_WS_URL = "ws://localhost:5000/v1/api/ws"
_DEFAULT_ACCOUNT = "U25235077"
_DEFAULT_DURATION_S = 60.0
_DEFAULT_SOR_DURATION_S = 30.0
_DEFAULT_RECONNECT_PHASE_S = 30.0


# Bekannte stabile conids fuer Top-US-Werte. Aus AP-02-Recordings und
# secdef/search in frueheren Laeufen extrahiert. Nicht erschoepfend -
# fehlende Symbole werden zur Laufzeit per /iserver/secdef/search
# aufgeloest.
_KNOWN_CONIDS: dict[str, int] = {
    "AAPL": 265598,
    "MSFT": 272093,
    "AMZN": 3691937,
    "GOOGL": 208813720,
    "META": 107113386,
    "NVDA": 4815747,
    "TSLA": 76792991,
    "JPM": 8595,
    "V": 80268543,
    "MA": 38708077,
    "SAP": 104747,
}


_TOP_5_SYMBOLS: tuple[str, ...] = ("AAPL", "MSFT", "AMZN", "GOOGL", "META")
_TOP_25_SYMBOLS: tuple[str, ...] = (
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "TSLA", "JPM",
    "V", "MA", "JNJ", "WMT", "PG", "UNH", "HD", "DIS", "BAC", "VZ",
    "ADBE", "NFLX", "CRM", "INTC", "CSCO", "PEP", "KO",
)
_SMD_FIELDS: tuple[str, ...] = ("31", "84", "86", "6509", "83")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JsonlSink:
    """Schreibt Frames im K2-kanonischen Format als JSONL."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._fh = path.open("a", encoding="utf-8")
        self._frame_count = 0

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        dir_: str,
        topic: str,
        raw: str,
        parsed: Any | None = None,
    ) -> None:
        entry = {
            "ts": _utcnow_iso(),
            "dir": dir_,
            "topic": topic,
            "raw": raw,
            "parsed": parsed,
        }
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()
        self._frame_count += 1

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Voraussetzungs- und Session-Helpers
# ---------------------------------------------------------------------------


class PreconditionError(RuntimeError):
    """Voraussetzungs-Check vor dem Run ist fehlgeschlagen."""


async def _check_auth_and_fetch_session(
    base_url: str,
) -> tuple[str, str]:
    """Pruef auth-status und liefert ``(session_id, cookie_header)``."""
    async with httpx.AsyncClient(
        base_url=base_url, timeout=10.0
    ) as client:
        try:
            r = await client.get("/v1/api/iserver/auth/status")
        except httpx.HTTPError as exc:
            raise PreconditionError(
                f"CP-Gateway nicht erreichbar unter {base_url}: {exc}"
            ) from exc
        if r.status_code != 200:
            raise PreconditionError(
                f"auth/status liefert HTTP {r.status_code}: {r.text[:200]}"
            )
        body = r.json()
        if not body.get("authenticated"):
            raise PreconditionError(
                "Session nicht authenticated - Browser-Login durchfuehren "
                "(docs/runbooks/cpgateway-login.md)."
            )
        if body.get("competing"):
            raise PreconditionError(
                "auth/status meldet competing=true - parallele Session "
                "beenden bevor das Skript laeuft."
            )

        # IBKR-Quirk: /iserver/accounts vor account-spezifischen Calls.
        await client.get("/v1/api/iserver/accounts")

        r = await client.post("/v1/api/tickle")
        r.raise_for_status()
        session_id = r.json().get("session")
        if not session_id:
            raise PreconditionError(
                "/tickle liefert kein session-Feld."
            )
        cookie_header = "; ".join(
            f"{c.name}={c.value}" for c in client.cookies.jar
        )
        return session_id, cookie_header


async def _resolve_conids(
    base_url: str, symbols: list[str]
) -> dict[str, int]:
    """Loest fehlende Symbole via /iserver/secdef/search auf."""
    out = {s: _KNOWN_CONIDS[s] for s in symbols if s in _KNOWN_CONIDS}
    missing = [s for s in symbols if s not in out]
    if not missing:
        return out
    async with httpx.AsyncClient(
        base_url=base_url, timeout=10.0
    ) as client:
        for sym in missing:
            try:
                r = await client.get(
                    "/v1/api/iserver/secdef/search",
                    params={"symbol": sym},
                )
            except httpx.HTTPError as exc:
                print(
                    f"  [warn] secdef/search {sym} fehlgeschlagen: {exc}",
                    file=sys.stderr,
                )
                continue
            if r.status_code != 200:
                continue
            try:
                payload = r.json()
            except ValueError:
                continue
            if isinstance(payload, list) and payload:
                cid = payload[0].get("conid")
                if cid is not None:
                    out[sym] = int(cid)
    return out


# ---------------------------------------------------------------------------
# Recorder: WS-Client + JsonlSink kombiniert
# ---------------------------------------------------------------------------


class _Recorder:
    """Bindet einen CPWebSocketClient an einen JsonlSink."""

    def __init__(self, sink: JsonlSink, ws_url: str) -> None:
        self._sink = sink
        self._ws_url = ws_url
        self._client: CPWebSocketClient | None = None

    async def connect(
        self, session_id: str, cookies: str | None
    ) -> CPWebSocketClient:
        self._sink.write(
            "meta",
            "connect",
            self._ws_url,
            parsed={"session_id": "<redacted>"},
        )
        client = CPWebSocketClient(url=self._ws_url)
        # Auth-Frame wird vom Client intern gesendet; im Mitschnitt
        # explizit als out-Frame protokollieren, damit das Format zu
        # spike-baseline.jsonl konsistent bleibt.
        self._sink.write(
            "out",
            "auth",
            json.dumps({"session": session_id}),
            parsed={"session": "<redacted>"},
        )
        await client.connect(session_id, cookies=cookies)
        self._client = client
        return client

    async def send(
        self, frame: str, topic: str, parsed_meta: Any | None = None
    ) -> None:
        if self._client is None:
            raise RuntimeError("send vor connect")
        self._sink.write("out", topic, frame, parsed=parsed_meta)
        await self._client.send(frame)

    async def listen_for(self, duration_s: float) -> int:
        """Liest Frames bis ``duration_s`` Wallclock erreicht."""
        if self._client is None:
            raise RuntimeError("listen_for vor connect")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration_s
        captured = 0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(
                    self._client.__anext__(), timeout=remaining
                )
            except asyncio.TimeoutError:
                break
            except StopAsyncIteration:
                self._sink.write(
                    "meta",
                    "stream-ended",
                    "iterator endete vor deadline",
                )
                break
            # CPWebSocketClient gibt raw als ``str`` typisiert zurueck, der
            # darunterliegende websockets.recv() kann jedoch ``bytes``
            # liefern. Robuste Konvertierung hier - der src/-Code bleibt
            # laut Karten-Constraint unangetastet.
            raw = frame.raw
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            self._sink.write("in", frame.topic, raw, parsed=frame.parsed)
            captured += 1
        return captured

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._sink.write("meta", "disconnect", "")


# ---------------------------------------------------------------------------
# Subscribe-Frame-Builder
# ---------------------------------------------------------------------------


def _smd_frame(conid: int, fields: tuple[str, ...] = _SMD_FIELDS) -> str:
    body = json.dumps({"fields": list(fields)}, separators=(",", ":"))
    return f"smd+{conid}+{body}"


def _umd_frame(conid: int) -> str:
    return f"umd+{conid}+{{}}"


def _sor_frame() -> str:
    return "sor+{}"


def _uor_frame() -> str:
    return "uor+{}"


def _str_frame(realtime_only: bool = False) -> str:
    body = json.dumps(
        {"realtimeUpdatesOnly": realtime_only}, separators=(",", ":")
    )
    return f"str+{body}"


def _utr_frame() -> str:
    return "utr"


# ---------------------------------------------------------------------------
# Szenarien
# ---------------------------------------------------------------------------


async def run_smd_single(
    args: argparse.Namespace, base_url: str, ws_url: str, out_dir: Path
) -> int:
    print(f"[smd-single] AAPL conid 265598, fields {_SMD_FIELDS}")
    sink = JsonlSink(out_dir / "smd-single.jsonl")
    session_id, cookies = await _check_auth_and_fetch_session(base_url)
    rec = _Recorder(sink, ws_url)
    try:
        await rec.connect(session_id, cookies)
        await rec.send(_smd_frame(265598), topic="smd")
        captured = await rec.listen_for(args.duration)
        print(f"[smd-single] {captured} Frames in {args.duration}s")
        await rec.send(_umd_frame(265598), topic="umd")
        await rec.listen_for(2.0)  # noch kurz lauschen ob ack
    finally:
        await rec.close()
        sink.close()
    return 0


async def run_smd_multi(
    args: argparse.Namespace, base_url: str, ws_url: str, out_dir: Path
) -> int:
    symbols = list(_TOP_5_SYMBOLS)
    print(f"[smd-multi] {len(symbols)} Symbole: {symbols}")
    conids = await _resolve_conids(base_url, symbols)
    print(f"[smd-multi] conids: {conids}")
    sink = JsonlSink(out_dir / "smd-multi.jsonl")
    session_id, cookies = await _check_auth_and_fetch_session(base_url)
    rec = _Recorder(sink, ws_url)
    try:
        await rec.connect(session_id, cookies)
        for sym, cid in conids.items():
            await rec.send(_smd_frame(cid), topic="smd", parsed_meta={"sym": sym})
        captured = await rec.listen_for(args.duration)
        print(f"[smd-multi] {captured} Frames in {args.duration}s")
        for cid in conids.values():
            await rec.send(_umd_frame(cid), topic="umd")
        await rec.listen_for(2.0)
    finally:
        await rec.close()
        sink.close()
    return 0


async def run_smd_large(
    args: argparse.Namespace, base_url: str, ws_url: str, out_dir: Path
) -> int:
    symbols = list(_TOP_25_SYMBOLS)
    print(f"[smd-large] {len(symbols)} Symbole: {symbols}")
    conids = await _resolve_conids(base_url, symbols)
    if len(conids) < 20:
        print(
            f"  [warn] Nur {len(conids)}/{len(symbols)} conids aufgeloest - "
            "trotzdem fortfahren."
        )
    print(f"[smd-large] conids aufgeloest: {len(conids)}")
    sink = JsonlSink(out_dir / "smd-large.jsonl")
    session_id, cookies = await _check_auth_and_fetch_session(base_url)
    rec = _Recorder(sink, ws_url)
    try:
        await rec.connect(session_id, cookies)
        # Schnell hintereinander subscriben - das ist der Pacing-Test.
        for sym, cid in conids.items():
            await rec.send(
                _smd_frame(cid), topic="smd", parsed_meta={"sym": sym}
            )
        captured = await rec.listen_for(args.duration)
        print(f"[smd-large] {captured} Frames in {args.duration}s")
        # Pacing-Hinweise erkennen: wir scannen die Sink-Datei.
        if _detect_pacing_violation(sink.path):
            print(
                "  [hit] Pacing-Hinweis im Mitschnitt erkannt - in Findings "
                "dokumentieren."
            )
        for cid in conids.values():
            await rec.send(_umd_frame(cid), topic="umd")
        await rec.listen_for(2.0)
    finally:
        await rec.close()
        sink.close()
    return 0


def _detect_pacing_violation(jsonl_path: Path) -> bool:
    """Sucht nach IBKR-Pacing-Codes in den mitgeschnittenen Frames."""
    if not jsonl_path.is_file():
        return False
    keywords = ("pacing", "PACING", "violation", "throttle")
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            for kw in keywords:
                if kw in line:
                    return True
    return False


async def run_str_trades(
    args: argparse.Namespace, base_url: str, ws_url: str, out_dir: Path
) -> int:
    print("[str-trades] subscribe str+{realtimeUpdatesOnly:false}")
    sink = JsonlSink(out_dir / "str.jsonl")
    session_id, cookies = await _check_auth_and_fetch_session(base_url)
    rec = _Recorder(sink, ws_url)
    try:
        await rec.connect(session_id, cookies)
        await rec.send(_str_frame(realtime_only=False), topic="str")
        captured = await rec.listen_for(args.duration)
        print(f"[str-trades] {captured} Frames in {args.duration}s")
        await rec.send(_utr_frame(), topic="utr")
        await rec.listen_for(2.0)
    finally:
        await rec.close()
        sink.close()
    return 0


async def run_sor(
    args: argparse.Namespace, base_url: str, ws_url: str, out_dir: Path
) -> int:
    print("[sor] subscribe sor+{}")
    sink = JsonlSink(out_dir / "sor.jsonl")
    session_id, cookies = await _check_auth_and_fetch_session(base_url)
    rec = _Recorder(sink, ws_url)
    try:
        await rec.connect(session_id, cookies)
        await rec.send(_sor_frame(), topic="sor")
        # Phase 1: lauschen ohne Aktion
        print(f"[sor] Phase 1 - {args.duration}s lauschen ohne Order-Aktion")
        captured1 = await rec.listen_for(args.duration)
        print(f"[sor] Phase 1: {captured1} Frames")
        if args.with_test_order:
            print("[sor] Phase 2 - Live-Test-Order place + cancel")
            order_count = await _place_and_cancel_test_order(
                base_url, args.account_id, sink
            )
            # Phase 3: nach Order-Aktion 30s mehr lauschen
            captured3 = await rec.listen_for(args.duration)
            print(
                f"[sor] Phase 3: {captured3} Frames (Order-Aktion: "
                f"{order_count} REST-Calls)"
            )
        else:
            print("[sor] --with-test-order nicht gesetzt: Phase 2/3 skipped")
        await rec.send(_uor_frame(), topic="uor")
        await rec.listen_for(2.0)
    finally:
        await rec.close()
        sink.close()
    return 0


async def _place_and_cancel_test_order(
    base_url: str, account_id: str, sink: JsonlSink
) -> int:
    """Plaziert eine winzige LMT-Order weit weg vom Markt und canceled
    sie sofort. Schreibt Meta-Eintraege ins Sink."""
    body = {
        "orders": [
            {
                "conid": _KNOWN_CONIDS["AAPL"],
                "orderType": "LMT",
                "side": "BUY",
                "quantity": 1,
                "price": 1.00,  # weit unter Marktpreis - matched garantiert nicht
                "tif": "DAY",
            }
        ]
    }
    calls = 0
    async with httpx.AsyncClient(
        base_url=base_url, timeout=15.0
    ) as client:
        sink.write(
            "meta",
            "rest-place-attempt",
            f"POST /v1/api/iserver/account/{account_id}/orders",
            parsed=body,
        )
        try:
            r = await client.post(
                f"/v1/api/iserver/account/{account_id}/orders", json=body
            )
            calls += 1
        except httpx.HTTPError as exc:
            sink.write("meta", "rest-place-error", str(exc))
            return calls
        sink.write(
            "meta",
            "rest-place-response",
            r.text[:500],
            parsed={"status_code": r.status_code},
        )
        # Wenn IBKR Bestaetigungs-Reply schickt, muessen wir confirm zurueckschicken.
        # Bestaetigungs-Antworten haben keinen order_id, sondern message+id.
        order_ids: list[str] = []
        try:
            payload = r.json()
        except ValueError:
            payload = []
        # IBKR-Confirmations-Schleife: solange der Response messages enthaelt
        # die der Server bestaetigt haben will, /reply/{id} mit confirmed=true
        # zuruecksenden.
        while True:
            if not isinstance(payload, list) or not payload:
                break
            first = payload[0]
            if not isinstance(first, dict):
                break
            if "id" in first and "message" in first:
                # Confirmation noetig
                reply_id = first["id"]
                sink.write(
                    "meta",
                    "rest-reply",
                    f"POST /v1/api/iserver/reply/{reply_id} confirmed=true",
                    parsed={"reply_id": reply_id},
                )
                try:
                    rr = await client.post(
                        f"/v1/api/iserver/reply/{reply_id}",
                        json={"confirmed": True},
                    )
                    calls += 1
                except httpx.HTTPError as exc:
                    sink.write("meta", "rest-reply-error", str(exc))
                    return calls
                sink.write(
                    "meta",
                    "rest-reply-response",
                    rr.text[:500],
                    parsed={"status_code": rr.status_code},
                )
                try:
                    payload = rr.json()
                except ValueError:
                    break
                continue
            # order_id im Response
            for entry in payload:
                if isinstance(entry, dict):
                    oid = entry.get("order_id") or entry.get("orderId")
                    if oid:
                        order_ids.append(str(oid))
            break
        sink.write(
            "meta",
            "rest-order-ids",
            json.dumps(order_ids),
            parsed={"order_ids": order_ids},
        )
        # Sofort cancellen
        for oid in order_ids:
            sink.write(
                "meta",
                "rest-cancel-attempt",
                f"DELETE /v1/api/iserver/account/{account_id}/order/{oid}",
            )
            try:
                rc = await client.delete(
                    f"/v1/api/iserver/account/{account_id}/order/{oid}"
                )
                calls += 1
            except httpx.HTTPError as exc:
                sink.write("meta", "rest-cancel-error", str(exc))
                continue
            sink.write(
                "meta",
                "rest-cancel-response",
                rc.text[:500],
                parsed={"status_code": rc.status_code},
            )
    return calls


async def run_reconnect(
    args: argparse.Namespace, base_url: str, ws_url: str, out_dir: Path
) -> int:
    """Phase 1: subscribe smd, X s lauschen, dann Verbindung aktiv schliessen.
    Phase 2: neue Verbindung mit gleicher session-id, ohne Subscribe, X s
    lauschen - kommen smd-Frames trotzdem?"""
    print("[reconnect] Phase 1 - subscribe + Verbindung aktiv schliessen")
    sink = JsonlSink(out_dir / "reconnect.jsonl")
    session_id, cookies = await _check_auth_and_fetch_session(base_url)
    rec1 = _Recorder(sink, ws_url)
    try:
        await rec1.connect(session_id, cookies)
        await rec1.send(_smd_frame(265598), topic="smd")
        captured1 = await rec1.listen_for(args.phase1_duration)
        print(
            f"[reconnect] Phase 1: {captured1} Frames in "
            f"{args.phase1_duration}s"
        )
    finally:
        await rec1.close()

    sink.write(
        "meta",
        "phase-boundary",
        f"Verbindung 1 geschlossen, Pause {args.gap_s}s",
    )
    await asyncio.sleep(args.gap_s)

    print(
        "[reconnect] Phase 2 - neue Verbindung, KEIN neuer subscribe, "
        f"{args.phase2_duration}s lauschen"
    )
    # Frische Session holen - nach close kann die alte session-id "verbraucht"
    # sein. Der Test interessiert sich fuer Subscription-Persistence, nicht
    # fuer Session-Persistence.
    session_id2, cookies2 = await _check_auth_and_fetch_session(base_url)
    rec2 = _Recorder(sink, ws_url)
    try:
        await rec2.connect(session_id2, cookies2)
        captured2 = await rec2.listen_for(args.phase2_duration)
        print(
            f"[reconnect] Phase 2: {captured2} Frames "
            "(ohne neuen subscribe)"
        )
    finally:
        await rec2.close()
        sink.close()
    return 0


async def run_all(
    args: argparse.Namespace, base_url: str, ws_url: str, out_dir: Path
) -> int:
    """Default-Reihenfolge: smd-Single, smd-Multi, smd-Large, str, reconnect.
    sor wird im 'all'-Run NICHT automatisch ausgefuehrt - separater Aufruf
    mit --with-test-order erforderlich."""
    rc = 0
    for name, fn in (
        ("smd-single", run_smd_single),
        ("smd-multi", run_smd_multi),
        ("smd-large", run_smd_large),
        ("str-trades", run_str_trades),
        ("reconnect", run_reconnect),
    ):
        print(f"\n=== {name} ===")
        try:
            rc = await fn(args, base_url, ws_url, out_dir) or rc
        except Exception as exc:  # noqa: BLE001
            print(
                f"[{name}] FEHLER: {exc!r} - Szenario abgebrochen, naechstes",
                file=sys.stderr,
            )
            rc = 1
        # Kleine Pause zwischen Szenarien, damit IBKR nicht meint wir
        # haetten mehrere parallele WS-Sessions.
        await asyncio.sleep(2.0)
    return rc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "WS-Topic-Explorer fuer K4 (AP-04). Live-Mitschnitt smd/sor/str."
        )
    )
    parser.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help=f"REST-Base-URL (default: {_DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--ws-url",
        default=_DEFAULT_WS_URL,
        help=f"WebSocket-URL (default: {_DEFAULT_WS_URL}).",
    )
    parser.add_argument(
        "--account-id",
        default=_DEFAULT_ACCOUNT,
        help=f"IBKR-Konto (default: {_DEFAULT_ACCOUNT}).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=(
            "Zielverzeichnis (default: tests/fixtures/recorded/ws/"
            "topic-explorer-YYYY-MM-DD/)."
        ),
    )
    sub = parser.add_subparsers(dest="scenario", required=True)

    p_smd_single = sub.add_parser("smd-single")
    p_smd_single.add_argument(
        "--duration", type=float, default=_DEFAULT_DURATION_S
    )

    p_smd_multi = sub.add_parser("smd-multi")
    p_smd_multi.add_argument(
        "--duration", type=float, default=_DEFAULT_DURATION_S
    )

    p_smd_large = sub.add_parser("smd-large")
    p_smd_large.add_argument(
        "--duration", type=float, default=_DEFAULT_DURATION_S
    )

    p_str = sub.add_parser("str-trades")
    p_str.add_argument(
        "--duration", type=float, default=_DEFAULT_DURATION_S
    )

    p_sor = sub.add_parser("sor")
    p_sor.add_argument(
        "--duration", type=float, default=_DEFAULT_SOR_DURATION_S
    )
    p_sor.add_argument(
        "--with-test-order",
        action="store_true",
        help=(
            "Plaziert eine winzige LMT-Order auf AAPL (1 Stueck, $1) und "
            "canceled sie sofort. Match unwahrscheinlich (Preis weit unter "
            "Markt), aber Live-Account - bewusst einsetzen."
        ),
    )

    p_rec = sub.add_parser("reconnect")
    p_rec.add_argument(
        "--phase1-duration", type=float, default=_DEFAULT_RECONNECT_PHASE_S
    )
    p_rec.add_argument(
        "--phase2-duration", type=float, default=_DEFAULT_RECONNECT_PHASE_S
    )
    p_rec.add_argument("--gap-s", type=float, default=3.0)

    p_all = sub.add_parser("all")
    p_all.add_argument(
        "--duration", type=float, default=_DEFAULT_DURATION_S
    )
    p_all.add_argument(
        "--phase1-duration", type=float, default=_DEFAULT_RECONNECT_PHASE_S
    )
    p_all.add_argument(
        "--phase2-duration", type=float, default=_DEFAULT_RECONNECT_PHASE_S
    )
    p_all.add_argument("--gap-s", type=float, default=3.0)

    return parser


def _resolve_out_dir(args: argparse.Namespace) -> Path:
    if args.out_dir is not None:
        return args.out_dir
    today = date.today().isoformat()
    return Path("tests/fixtures/recorded/ws") / f"topic-explorer-{today}"


_DISPATCH = {
    "smd-single": run_smd_single,
    "smd-multi": run_smd_multi,
    "smd-large": run_smd_large,
    "str-trades": run_str_trades,
    "sor": run_sor,
    "reconnect": run_reconnect,
    "all": run_all,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    out_dir = _resolve_out_dir(args)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[setup] base-url   = {args.base_url}")
    print(f"[setup] ws-url     = {args.ws_url}")
    print(f"[setup] account-id = {args.account_id}")
    print(f"[setup] out-dir    = {out_dir.resolve()}")
    print(f"[setup] scenario   = {args.scenario}")

    fn = _DISPATCH[args.scenario]
    try:
        return asyncio.run(fn(args, args.base_url, args.ws_url, out_dir))
    except PreconditionError as exc:
        print(f"VORAUSSETZUNG FEHLT: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("[abort] Strg+C", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
