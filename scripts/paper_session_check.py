#!/usr/bin/env python3
"""Smoke-Skript fuer den Paper-Stack (AP-06 K4).

Drei Probes gegen den deployed broker-gateway-paper:

1. ``GET /v1/health`` - Service lebt.
2. ``GET /v1/internal/health`` mit Bootstrap-Token - CP-Gateway-
   Konnektivitaet, ``account_id`` mit ``DU``-Praefix.
3. ``GET /v1/instruments/search?symbol=AAPL`` - Token-/Scope-Pfad und
   CP-Gateway-Roundtrip funktionieren.

Exit:
- 0 = alle Probes 200 OK plus DU-Account-ID.
- 1 = mindestens eine Probe schlug fehl. Diagnose pro Probe auf stderr.
- 2 = Konfigurationsfehler (BG_PAPER_BASE_URL fehlt).

Konfiguration ueber ENV:
- ``BG_PAPER_BASE_URL`` (Pflicht): z.B. ``http://cma-pi-1:4001``.
- ``BG_PAPER_BOOTSTRAP_TOKEN`` (Pflicht fuer Probe 2/3): aus
  ``.env.paper`` BG_BOOTSTRAP_ADMIN_TOKEN.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)


def _probe_health(base_url: str) -> bool:
    url = f"{base_url}/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            if response.status != 200:
                _fail(f"GET {url} -> HTTP {response.status}")
                return False
            payload = json.loads(response.read())
            if payload.get("status") != "ok":
                _fail(f"GET {url} -> body {payload!r}")
                return False
            print(f"OK   GET {url} -> status=ok version={payload.get('version')}")
            return True
    except urllib.error.URLError as exc:
        _fail(f"GET {url} -> {exc}")
        return False


def _probe_internal_health(base_url: str, token: str) -> tuple[bool, str | None]:
    url = f"{base_url}/v1/internal/health"
    try:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                _fail(f"GET {url} -> HTTP {response.status}")
                return False, None
            payload = json.loads(response.read())
            account = payload.get("account_id") or (
                payload.get("accounts") or [None]
            )[0]
            if not account or not str(account).startswith("DU"):
                _fail(
                    f"GET {url} -> account_id={account!r} hat keinen "
                    "DU-Praefix (Paper-Konto erwartet)"
                )
                return False, account
            print(
                f"OK   GET {url} -> session_status={payload.get('session_status')!r} "
                f"account_id={account}"
            )
            return True, account
    except urllib.error.HTTPError as exc:
        _fail(f"GET {url} -> HTTP {exc.code} {exc.reason}")
        return False, None
    except urllib.error.URLError as exc:
        _fail(f"GET {url} -> {exc}")
        return False, None


def _probe_instruments_search(base_url: str, token: str) -> bool:
    url = f"{base_url}/v1/instruments/search?symbol=AAPL"
    try:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                _fail(f"GET {url} -> HTTP {response.status}")
                return False
            payload = json.loads(response.read())
            if not isinstance(payload, list) or not payload:
                _fail(f"GET {url} -> body {payload!r}")
                return False
            first = payload[0]
            print(
                f"OK   GET {url} -> conid={first.get('conid')} "
                f"symbol={first.get('symbol')}"
            )
            return True
    except urllib.error.HTTPError as exc:
        _fail(f"GET {url} -> HTTP {exc.code} {exc.reason}")
        return False
    except urllib.error.URLError as exc:
        _fail(f"GET {url} -> {exc}")
        return False


def main() -> int:
    base_url = os.environ.get("BG_PAPER_BASE_URL")
    token = os.environ.get("BG_PAPER_BOOTSTRAP_TOKEN", "")
    if not base_url:
        _fail(
            "BG_PAPER_BASE_URL nicht gesetzt. Beispiel:\n"
            "  BG_PAPER_BASE_URL=http://cma-pi-1:4001 "
            "BG_PAPER_BOOTSTRAP_TOKEN=$(...) python3 scripts/paper_session_check.py"
        )
        return 2

    base_url = base_url.rstrip("/")
    failures = 0

    if not _probe_health(base_url):
        failures += 1

    if not token:
        _fail(
            "BG_PAPER_BOOTSTRAP_TOKEN nicht gesetzt - Probes 2 und 3 "
            "uebersprungen. Token aus .env.paper "
            "(BG_BOOTSTRAP_ADMIN_TOKEN) verwenden."
        )
        return 1

    ok, _account = _probe_internal_health(base_url, token)
    if not ok:
        failures += 1

    if not _probe_instruments_search(base_url, token):
        failures += 1

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
