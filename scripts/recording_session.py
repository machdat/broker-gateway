#!/usr/bin/env python3
"""Live-Recording-Session gegen das interne IBKR Client Portal Gateway.

Subkommandos:

- ``happy-path`` (AP-02 #04): ruft alle v1-Endpunkte sequenziell ab
  und legt JSON-Fixtures unter ``--record-dir`` ab. Order-Test laeuft
  ueber ``/orders/whatif`` (Preview, IBKR plaziert nichts) - mit
  optionalem Place+sofortigem Cancel als zusaetzlicher Schritt
  (``--with-place-cancel``, gefragt vor Ausfuehrung).
- ``error-path`` (AP-02 #05): folgt in Karte 05.

Authentifizierung wird **vorher** gegen ``/iserver/auth/status``
geprueft. Wenn ``authenticated=false``, bricht das Skript mit einem
Hinweis auf das Login-Runbook ab - kein automatischer 2FA-Trigger,
das laeuft per Browser ueber den SSH-Reverse-Tunnel.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

import httpx

from broker_gateway import __version__ as bg_version
from broker_gateway.cp.recorder import CPRecorder


_DEFAULT_BASE_URL = "http://localhost:5000/v1/api"
_DEFAULT_RECORD_DIR = Path("tests/fixtures/recorded/live")
_DEFAULT_ACCOUNT = "U25235077"
_DEFAULT_SYMBOLS: tuple[str, ...] = ("AAPL", "MSFT", "SAP")
_PRECONDITION_TIMEOUT_S = 10.0
_REQUEST_TIMEOUT_S = 30.0


class PreconditionError(RuntimeError):
    """Voraussetzungs-Check vor dem Recording-Lauf ist fehlgeschlagen."""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recording_session",
        description=(
            "Live-Recording-Session gegen das interne IBKR Client Portal "
            "Gateway. Schreibt JSON-Fixtures fuer den Replay-Mock."
        ),
    )
    sub = p.add_subparsers(dest="scenario", required=True)

    happy = sub.add_parser(
        "happy-path",
        help="AP-02 #04: alle v1-Endpunkte einmal aufzeichnen.",
    )
    _add_common_args(happy)
    happy.add_argument(
        "--symbols",
        nargs="+",
        default=list(_DEFAULT_SYMBOLS),
        help="Symbole fuer Lookup/Quotes (default: AAPL MSFT SAP).",
    )
    happy.add_argument(
        "--with-place-cancel",
        action="store_true",
        help=(
            "Optionaler Variante-B-Schritt: nach erfolgreichem whatif "
            "eine winzige Order tatsaechlich plazieren und sofort wieder "
            "canceln. Skript fragt nochmal explizit per Konsole."
        ),
    )
    happy.add_argument(
        "--skip-orders",
        action="store_true",
        help="Order-Schritte (whatif + optional place/cancel) ueberspringen.",
    )
    happy.add_argument(
        "--yes",
        action="store_true",
        help="Konsolen-Abfrage vor dem Order-Schritt unterdruecken (CI/Skript-Modus).",
    )

    error = sub.add_parser(
        "error-path",
        help="AP-02 #05: Error-Path - provoziert IBKR-Fehler und zeichnet sie auf.",
    )
    _add_common_args(error)
    error.add_argument(
        "--with-reauth-fail",
        action="store_true",
        help=(
            "Zerstoert die Session via /logout und ruft danach /reauthenticate "
            "auf, um den auth-lost-Body aufzuzeichnen. Erfordert anschliessend "
            "neuen Browser-Login (siehe docs/runbooks/recording-session-error-path.md)."
        ),
    )
    error.add_argument(
        "--yes",
        action="store_true",
        help="Konsolen-Abfragen unterdruecken.",
    )

    legacy_skel = sub.add_parser(
        "skeleton",
        help="Frueheres Skelett (nur Konfig drucken). Bleibt fuer Backwards-Compat.",
    )
    _add_common_args(legacy_skel)

    return p


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--record-dir",
        type=Path,
        default=_DEFAULT_RECORD_DIR,
        help=f"Zielverzeichnis (default: {_DEFAULT_RECORD_DIR}).",
    )
    p.add_argument(
        "--base-url",
        default=_DEFAULT_BASE_URL,
        help=f"CP-Gateway Base-URL (default: {_DEFAULT_BASE_URL}).",
    )
    p.add_argument(
        "--account-id",
        default=_DEFAULT_ACCOUNT,
        help=f"IBKR-Konto (default: {_DEFAULT_ACCOUNT}).",
    )
    p.add_argument(
        "--normalize-prices",
        action="store_true",
        help="Auch Preise/MarketData durch Platzhalter ersetzen (default: aus).",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if args.scenario == "skeleton":
        return _legacy_skeleton(args)
    if args.scenario == "error-path":
        return asyncio.run(run_error_path(args))
    return asyncio.run(run_happy_path(args))


def _legacy_skeleton(args: argparse.Namespace) -> int:
    print("Recording-Session-Konfiguration:")
    print(f"  record_dir        = {Path(args.record_dir).resolve()}")
    print(f"  base_url          = {args.base_url}")
    print(f"  account_id        = {args.account_id}")
    print(f"  scenario          = {args.scenario}")
    print(f"  normalize_prices  = {args.normalize_prices}")
    return 0


# ---- Voraussetzungs-Checks ----

async def check_preconditions(client: httpx.AsyncClient) -> dict[str, Any]:
    """Liefert auth-status oder wirft PreconditionError mit Hinweis."""
    try:
        resp = await client.get(
            "/iserver/auth/status", timeout=_PRECONDITION_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        raise PreconditionError(
            f"CP-Gateway nicht erreichbar unter {client.base_url}: {exc}. "
            "Pruefen: laeuft der cpgateway-Container, ist der SSH-Tunnel offen?"
        ) from exc

    if resp.status_code == 401:
        raise PreconditionError(
            "CP-Gateway antwortet 401 auf /iserver/auth/status - kein "
            "Browser-Login erfolgt. Siehe docs/runbooks/cpgateway-login.md."
        )
    if resp.status_code != 200:
        raise PreconditionError(
            f"Unerwarteter Status {resp.status_code} auf /iserver/auth/status: "
            f"{resp.text[:200]}"
        )

    body = resp.json()
    if not body.get("authenticated"):
        raise PreconditionError(
            "Session nicht authenticated (auth/status: "
            f"{json.dumps(body, sort_keys=True)}). "
            "Browser-Login durchfuehren - siehe docs/runbooks/cpgateway-login.md."
        )
    if body.get("competing"):
        raise PreconditionError(
            "auth/status meldet competing=true - eine andere Session ist gerade "
            "aktiv. Konkurrenz-Session beenden, bevor das Skript laeuft."
        )
    return body


# ---- Endpunkt-Sequenz ----

async def run_happy_path(args: argparse.Namespace) -> int:
    record_dir = Path(args.record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)

    print(f"[happy-path] record_dir = {record_dir.resolve()}")
    print(f"[happy-path] base_url   = {args.base_url}")
    print(f"[happy-path] account    = {args.account_id}")
    print(f"[happy-path] symbols    = {', '.join(args.symbols)}")
    print(f"[happy-path] place+cancel-Variante: "
          f"{'aktiviert' if args.with_place_cancel else 'aus'}")
    print()

    recorder = CPRecorder(record_dir, normalize_prices=args.normalize_prices)
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=_REQUEST_TIMEOUT_S
    ) as client:
        try:
            auth_body = await check_preconditions(client)
        except PreconditionError as exc:
            print(f"VORAUSSETZUNG FEHLT: {exc}", file=sys.stderr)
            return 3

        print(f"[ok] auth_status authenticated=true userId={auth_body.get('userId', '?')}")
        recorder.install_into(client)

        steps = _build_happy_path_steps(args)
        for label, step in steps:
            print(f"[run] {label}")
            try:
                await step(client)
            except httpx.HTTPError as exc:
                print(f"  [fail] {label}: {exc}", file=sys.stderr)

    manifest = _write_manifest(record_dir, args)
    print(f"\n[done] manifest: {manifest}")
    print(f"[done] {len(list(record_dir.glob('*.json'))) - 1} recordings unter {record_dir}.")
    return 0


def _build_happy_path_steps(
    args: argparse.Namespace,
) -> list[tuple[str, Callable[[httpx.AsyncClient], Awaitable[None]]]]:
    steps: list[tuple[str, Callable[[httpx.AsyncClient], Awaitable[None]]]] = []
    # Cache fuer aus search extrahierte conids - vermeidet doppelten search-
    # Aufruf in step d.
    conid_cache: dict[str, int] = {}

    # a) auth/status (zweiter Aufruf - wir wollen ihn auch im Recording haben)
    steps.append(("a) GET /iserver/auth/status", _step_auth_status))
    # a+) /iserver/accounts - IBKR-Quirk: muss vor account-spezifischen Calls
    # einmal aufgerufen werden, sonst antworten /portfolio/{acct}/* mit 404.
    steps.append(("a+) GET /iserver/accounts (Server-side accounts init)", _step_accounts_init))
    # b) tickle
    steps.append(("b) POST /tickle", _step_tickle))
    # c) secdef-search pro Symbol - cached conid fuer step d.
    for sym in args.symbols:
        steps.append(
            (f"c) GET /iserver/secdef/search?symbol={sym}",
             _step_secdef_search(sym, conid_cache)),
        )
    # d) secdef-info pro Symbol - conid aus cache (oder Fallback auf bekannte Tabelle).
    for sym in args.symbols:
        steps.append(
            (f"d) GET /iserver/secdef/info?conid=<{sym}>",
             _step_secdef_info(sym, conid_cache)),
        )
    # e) snapshot first/second call
    steps.append(("e) GET /iserver/marketdata/snapshot (prime, _01)", _step_snapshot_prime(args.symbols)))
    steps.append(("e) GET /iserver/marketdata/snapshot (values, _02)", _step_snapshot_prime(args.symbols)))
    # f) unsubscribe
    steps.append(("f) GET /iserver/marketdata/{conid}/unsubscribe", _step_unsubscribe(args.symbols)))
    # g/h/i) account - der Service-Code in cp/portfolio.py nutzt aktuell
    # /iserver/account/{acct}/{portfolio|positions|ledger}, aber IBKR's
    # echte REST-Pfade sind /portfolio/{acct}/{summary|positions/0|ledger}.
    # Wir zeichnen BEIDE Varianten auf, damit der Diff-Report den Bug
    # eindeutig belegen kann; eine Folgekarte stellt den Service-Code um.
    steps.append((f"g) GET /iserver/account/{args.account_id}/portfolio (Service-Pfad - ggf. 404)",
                  _step_get(f"/iserver/account/{args.account_id}/portfolio")))
    steps.append((f"g+) GET /portfolio/{args.account_id}/summary (echter IBKR-Pfad)",
                  _step_get(f"/portfolio/{args.account_id}/summary")))
    steps.append((f"h) GET /iserver/account/{args.account_id}/positions (Service-Pfad - ggf. 404)",
                  _step_get(f"/iserver/account/{args.account_id}/positions")))
    steps.append((f"h+) GET /portfolio/{args.account_id}/positions/0 (echter IBKR-Pfad)",
                  _step_get(f"/portfolio/{args.account_id}/positions/0")))
    steps.append((f"i) GET /iserver/account/{args.account_id}/ledger (Service-Pfad - ggf. 404)",
                  _step_get(f"/iserver/account/{args.account_id}/ledger")))
    steps.append((f"i+) GET /portfolio/{args.account_id}/ledger (echter IBKR-Pfad)",
                  _step_get(f"/portfolio/{args.account_id}/ledger")))
    # j) order-test (whatif)
    if not args.skip_orders:
        steps.append((f"j) POST /iserver/account/{args.account_id}/orders/whatif (preview)",
                      _step_whatif(args)))
        if args.with_place_cancel:
            steps.append((f"j+) POST /orders + DELETE /order/{{id}} (Variante B)",
                          _step_place_and_cancel(args)))
    # l) trades
    steps.append(("l) GET /iserver/account/trades?days=7", _step_trades))

    return steps


def _step_get(path: str) -> Callable[[httpx.AsyncClient], Awaitable[None]]:
    async def _go(client: httpx.AsyncClient) -> None:
        await client.get(path)
    return _go


async def _step_auth_status(client: httpx.AsyncClient) -> None:
    await client.get("/iserver/auth/status")


async def _step_accounts_init(client: httpx.AsyncClient) -> None:
    await client.get("/iserver/accounts")


async def _step_tickle(client: httpx.AsyncClient) -> None:
    await client.post("/tickle")


def _step_secdef_search(
    symbol: str, conid_cache: dict[str, int]
) -> Callable[[httpx.AsyncClient], Awaitable[None]]:
    async def _go(client: httpx.AsyncClient) -> None:
        resp = await client.get(
            "/iserver/secdef/search", params={"symbol": symbol}
        )
        if resp.status_code != 200:
            return
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            return
        if isinstance(payload, list) and payload:
            cid = payload[0].get("conid")
            if cid is not None:
                conid_cache[symbol] = int(cid)
    return _go


_FALLBACK_CONIDS: dict[str, int] = {"AAPL": 265598, "MSFT": 272093, "SAP": 104747}


def _step_secdef_info(
    symbol: str, conid_cache: dict[str, int]
) -> Callable[[httpx.AsyncClient], Awaitable[None]]:
    async def _go(client: httpx.AsyncClient) -> None:
        cid = conid_cache.get(symbol) or _FALLBACK_CONIDS.get(symbol)
        if cid is None:
            print(f"  [skip] keine conid fuer {symbol} - search lieferte nichts")
            return
        await client.get("/iserver/secdef/info", params={"conid": str(cid)})
    return _go


def _step_snapshot_prime(symbols: list[str]) -> Callable[[httpx.AsyncClient], Awaitable[None]]:
    async def _go(client: httpx.AsyncClient) -> None:
        # conids ueber search-Recordings nicht verfuegbar - fuer den Live-Lauf
        # nehmen wir die bekannten conids aus seed (sie sind bei IBKR stabil).
        conids = ",".join(_known_conids(symbols))
        await client.get(
            "/iserver/marketdata/snapshot",
            params={"conids": conids, "fields": "31,84,86,6509"},
        )
    return _go


def _step_unsubscribe(symbols: list[str]) -> Callable[[httpx.AsyncClient], Awaitable[None]]:
    async def _go(client: httpx.AsyncClient) -> None:
        for cid in _known_conids(symbols):
            await client.get(f"/iserver/marketdata/{cid}/unsubscribe")
    return _go


def _known_conids(symbols: list[str]) -> list[str]:
    table = {"AAPL": "265598", "MSFT": "272093", "SAP": "104747"}
    return [table[s] for s in symbols if s in table]


def _confirm_or_skip(prompt: str, *, yes: bool) -> bool:
    if yes:
        print(f"  [auto-yes] {prompt}")
        return True
    answer = input(f"  {prompt} [yes/no, default no]: ").strip().lower()
    return answer in {"y", "yes"}


def _step_whatif(args: argparse.Namespace) -> Callable[[httpx.AsyncClient], Awaitable[None]]:
    async def _go(client: httpx.AsyncClient) -> None:
        if not _confirm_or_skip(
            "Variante A (whatif Preview) ausfuehren?", yes=args.yes
        ):
            print("  [skip] whatif uebersprungen.")
            return
        body = {
            "orders": [
                {
                    "conid": int(_known_conids(["AAPL"])[0]),
                    "orderType": "MKT",
                    "side": "BUY",
                    "quantity": 1,
                    "tif": "DAY",
                }
            ]
        }
        await client.post(
            f"/iserver/account/{args.account_id}/orders/whatif",
            json=body,
        )
    return _go


def _step_place_and_cancel(
    args: argparse.Namespace,
) -> Callable[[httpx.AsyncClient], Awaitable[None]]:
    async def _go(client: httpx.AsyncClient) -> None:
        if not _confirm_or_skip(
            "Variante B (LIVE Place + sofort Cancel ausserhalb der Boersenzeiten)?",
            yes=args.yes,
        ):
            print("  [skip] place+cancel uebersprungen.")
            return
        body = {
            "orders": [
                {
                    "conid": int(_known_conids(["AAPL"])[0]),
                    "orderType": "LMT",
                    "side": "BUY",
                    "quantity": 1,
                    "price": 1.00,
                    "tif": "DAY",
                }
            ]
        }
        place = await client.post(
            f"/iserver/account/{args.account_id}/orders", json=body
        )
        order_ids: list[str] = []
        try:
            payload = place.json()
        except json.JSONDecodeError:
            payload = []
        if isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, dict):
                    oid = entry.get("order_id") or entry.get("orderId")
                    if oid:
                        order_ids.append(str(oid))
        # k) order-status fuer alle erhaltenen IDs
        for oid in order_ids:
            await client.get(f"/iserver/account/orders/{oid}")
            await client.delete(f"/iserver/account/{args.account_id}/order/{oid}")
            # zweite status-Abfrage: Cancelled-Bestaetigung
            await client.get(f"/iserver/account/orders/{oid}")
    return _go


async def _step_trades(client: httpx.AsyncClient) -> None:
    await client.get("/iserver/account/trades", params={"days": "7"})


# ---- Error-Path ----

async def run_error_path(args: argparse.Namespace) -> int:
    """Provoziert gezielt CP-Gateway-Fehlerfaelle und zeichnet sie auf.

    Cases (laut AP-02 #05):
      a. Pacing-Violation: 60 Snapshot-Calls in 1s.
      b. Ungueltige conid: secdef/info?conid=999999999.
      c. Ungueltige Order-Quantity: whatif mit qty=0.
      d. Nicht-existente Order-ID: /iserver/account/order/status/<bogus>.
      e. (optional, --with-reauth-fail) Auth-Lost: /logout dann /reauthenticate.
    """
    record_dir = Path(args.record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)

    print(f"[error-path] record_dir = {record_dir.resolve()}")
    print(f"[error-path] base_url   = {args.base_url}")
    print(f"[error-path] account    = {args.account_id}")
    print(f"[error-path] reauth-fail-Variante: "
          f"{'aktiviert (zerstoert Session!)' if args.with_reauth_fail else 'aus'}")
    print()

    recorder = CPRecorder(record_dir, normalize_prices=args.normalize_prices)
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=_REQUEST_TIMEOUT_S
    ) as client:
        try:
            auth_body = await check_preconditions(client)
        except PreconditionError as exc:
            print(f"VORAUSSETZUNG FEHLT: {exc}", file=sys.stderr)
            return 3
        print(f"[ok] auth_status authenticated=true userId={auth_body.get('userId', '?')}")

        recorder.install_into(client)

        # a) Pacing-Violation - viele schnelle Calls.
        print("[run] a) Pacing-Violation - 60 Snapshot-Calls in <1s")
        for i in range(60):
            try:
                resp = await client.get(
                    "/iserver/marketdata/snapshot",
                    params={"conids": "265598", "fields": "31,84,86,6509"},
                )
                if resp.status_code == 429:
                    print(f"  [hit] pacing-violation nach {i+1} Calls (HTTP 429)")
                    break
            except httpx.HTTPError as exc:
                print(f"  [fail] {exc}", file=sys.stderr)
                break
        else:
            print("  [miss] kein 429 nach 60 Calls - IBKR-Pacing greift moeglicherweise nicht")

        # b) Ungueltige conid.
        print("[run] b) Ungueltige conid 999999999")
        try:
            await client.get("/iserver/secdef/info", params={"conid": "999999999"})
        except httpx.HTTPError as exc:
            print(f"  [warn] {exc}")

        # c) Ungueltige Order-Quantity (whatif mit qty=0).
        if not _confirm_or_skip(
            "c) whatif mit qty=0 (Preview, IBKR plaziert nichts)?", yes=args.yes
        ):
            print("  [skip] whatif-quantity-fehler uebersprungen.")
        else:
            try:
                await client.post(
                    f"/iserver/account/{args.account_id}/orders/whatif",
                    json={"orders": [{
                        "conid": int(_known_conids(["AAPL"])[0]),
                        "orderType": "MKT",
                        "side": "BUY",
                        "quantity": 0,
                        "tif": "DAY",
                    }]},
                )
            except httpx.HTTPError as exc:
                print(f"  [warn] {exc}")

        # d) Nicht-existente Order-ID.
        print("[run] d) Nicht-existente Order-ID")
        try:
            await client.get("/iserver/account/order/status/999999999999")
        except httpx.HTTPError as exc:
            print(f"  [warn] {exc}")

        # e) Optional: Reauth-Fail durch /logout + /reauthenticate.
        if args.with_reauth_fail:
            if not _confirm_or_skip(
                "e) /logout dann /reauthenticate? Zerstoert die Session, neuer Browser-Login noetig!",
                yes=args.yes,
            ):
                print("  [skip] reauth-fail uebersprungen.")
            else:
                print("[run] e) /logout (zerstoert Session)")
                try:
                    await client.post("/logout")
                except httpx.HTTPError as exc:
                    print(f"  [warn] /logout: {exc}")
                print("[run] e) /reauthenticate (sollte fail liefern)")
                try:
                    await client.post("/reauthenticate")
                except httpx.HTTPError as exc:
                    print(f"  [warn] /reauthenticate: {exc}")
                print("[run] e) /iserver/auth/status (zeigt jetzt authenticated=false)")
                try:
                    await client.get("/iserver/auth/status")
                except httpx.HTTPError as exc:
                    print(f"  [warn] {exc}")

    manifest = _write_manifest(record_dir, args)
    print(f"\n[done] manifest: {manifest}")
    print(f"[done] {len(list(record_dir.glob('*.json'))) - 1} recordings unter {record_dir}.")
    if args.with_reauth_fail:
        print()
        print("ACHTUNG: Session ist jetzt zerstoert. Browser-2FA-Login erneut "
              "durchfuehren (docs/runbooks/cpgateway-login.md), bevor weitere "
              "Recordings oder produktive Calls erfolgen.")
    return 0


# ---- Manifest ----

def _write_manifest(record_dir: Path, args: argparse.Namespace) -> Path:
    files = sorted(p.name for p in record_dir.glob("*.json"))
    manifest_path = record_dir / "live-recording-manifest.json"
    manifest = {
        "scenario": args.scenario,
        "account_id": args.account_id,
        "base_url": args.base_url,
        "broker_gateway_version": bg_version,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "symbols": list(getattr(args, "symbols", [])),
        "files": [f for f in files if f != "live-recording-manifest.json"],
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path


if __name__ == "__main__":
    sys.exit(main())
