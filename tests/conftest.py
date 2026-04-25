"""Pytest-Fixtures für broker-gateway.

Single Source of Truth für Mock-Antworten gegen das interne IBKR Client
Portal Gateway. Folge-Karten dürfen keine eigenen Mocks definieren -
sie konfigurieren stattdessen Flags an `cp_gateway_mock`.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from typing import Any

import httpx
import pytest
import respx


def _to_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


# Default-Symbole und ihre conids (Werte realitätsnah, nicht echt verifiziert).
_INSTRUMENTS: dict[str, dict[str, Any]] = {
    "AAPL": {"conid": 265598, "companyName": "APPLE INC",      "currency": "USD", "exchange": "NASDAQ"},
    "MSFT": {"conid": 272093, "companyName": "MICROSOFT CORP", "currency": "USD", "exchange": "NASDAQ"},
    "SAP":  {"conid": 104747, "companyName": "SAP SE",         "currency": "EUR", "exchange": "IBIS"},
}
_BY_CONID: dict[int, dict[str, Any]] = {
    info["conid"]: {"symbol": sym, **info} for sym, info in _INSTRUMENTS.items()
}

# IBKR-Snapshot-Felder: 31=last, 84=bid, 86=ask, 6509=availability-code (DPB=delayed-paid-bidask).
_SNAPSHOT_VALUES: dict[int, dict[str, str]] = {
    265598: {"31": "150.50", "84": "150.45", "86": "150.55", "6509": "DPB"},
    272093: {"31": "320.10", "84": "320.05", "86": "320.15", "6509": "DPB"},
    104747: {"31": "120.20", "84": "120.15", "86": "120.25", "6509": "DPB"},
}


class MockCPGateway:
    """In-Memory-Mock des IBKR Client Portal Gateways.

    Standardverhalten ist immer der happy path. Abweichungen werden
    explizit per Flag konfiguriert.
    """

    def __init__(
        self,
        base_url: str = "http://cpgateway:5000/v1/api",
        *,
        auth_lost: bool = False,
        slow_response_ms: int = 0,
        pacing_violation_after_n: int | None = None,
        reply_warnings: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_lost = auth_lost
        self.slow_response_ms = slow_response_ms
        self.pacing_violation_after_n = pacing_violation_after_n
        # Liste von Warning-Confirmations, die place_order vor der echten
        # Order durchschiebt. Jedes Element ist ein Dict mit mindestens
        # `id` (UUID-aehnlich) und `message` (Liste von Warning-Strings).
        # Tests setzen das beim Bedarf, Default ist leer (= sofort
        # echte Order).
        self.reply_warnings: list[dict[str, Any]] = list(reply_warnings or [])
        # Wenn True, liefert _h_trades Trades ohne `currency`-Feld - testet
        # die Currency-Assumption-Logik in TradesService.
        self.omit_trade_currency: bool = False
        # Sequenz der unbestaetigten Warnings pro Order-Place-Aufruf:
        # mock fuegt sie in dieser Reihenfolge ein.
        self._pending_replies: dict[str, dict[str, Any]] = {}

        # Session-globaler State (für Refcount-Tests in späteren Karten relevant).
        self.subscriptions: set[int] = set()
        self.orders: dict[str, dict[str, Any]] = {}
        self.request_count: int = 0
        self._snapshot_calls: dict[tuple[str, str], int] = defaultdict(int)
        self._next_order_id: int = 1_000_000

    # ---- gemeinsame Pre-Hooks ----

    def _pre(self) -> httpx.Response | None:
        if self.slow_response_ms > 0:
            time.sleep(self.slow_response_ms / 1000.0)
        self.request_count += 1
        if (
            self.pacing_violation_after_n is not None
            and self.request_count > self.pacing_violation_after_n
        ):
            return httpx.Response(429, json={"error": "pacing-violation"})
        return None

    # ---- Auth ----

    def _h_auth_status(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        return httpx.Response(
            200,
            json={
                "authenticated": not self.auth_lost,
                "competing": False,
                "connected": True,
                "MAC": "MOCKED",
                "fail": "session-lost" if self.auth_lost else "",
            },
        )

    def _h_tickle(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        return httpx.Response(
            200,
            json={
                "session": "mock-session-id",
                "ssoExpires": 600_000,
                "collission": False,
                "userId": 123_456,
                "iserver": {
                    "authStatus": {
                        "authenticated": not self.auth_lost,
                        "connected": True,
                        "competing": False,
                    }
                },
            },
        )

    def _h_reauthenticate(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        # /reauthenticate triggert serverseitig - das Wiedererlangen der
        # Session bleibt asynchron. Tests, die auth-recovery prüfen, müssen
        # auth_lost selbst zurücksetzen, sobald sie reauth als "geklappt"
        # erwarten.
        return httpx.Response(200, json={"message": "triggered"})

    # ---- Sec-Def ----

    def _h_secdef_search(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        symbol = request.url.params.get("symbol", "").upper()
        info = _INSTRUMENTS.get(symbol)
        if info is None:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {
                    "conid": info["conid"],
                    "symbol": symbol,
                    "companyName": info["companyName"],
                    "description": info["currency"],
                    "secType": "STK",
                }
            ],
        )

    def _h_secdef_info(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        try:
            conid = int(request.url.params.get("conid", "0"))
        except ValueError:
            return httpx.Response(400, json={"error": "invalid conid"})
        info = _BY_CONID.get(conid)
        if info is None:
            return httpx.Response(404, json={"error": "unknown conid"})
        return httpx.Response(200, json=info)

    # ---- Marktdaten ----

    def _h_snapshot(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        conids_param = request.url.params.get("conids", "")
        fields_param = request.url.params.get("fields", "")
        key = (conids_param, fields_param)
        self._snapshot_calls[key] += 1
        call_n = self._snapshot_calls[key]

        requested_fields = [f for f in fields_param.split(",") if f]
        result: list[dict[str, Any]] = []
        for cid_str in conids_param.split(","):
            cid_str = cid_str.strip()
            if not cid_str:
                continue
            try:
                cid = int(cid_str)
            except ValueError:
                continue
            self.subscriptions.add(cid)
            entry: dict[str, Any] = {
                "conid": cid,
                "_updated": 1_700_000_000_000 + call_n,
            }
            # First-Call-Prime: erst ab dem zweiten Call sind Werte da.
            if call_n >= 2:
                values = _SNAPSHOT_VALUES.get(cid, {})
                for f in requested_fields:
                    if f in values:
                        entry[f] = values[f]
            result.append(entry)
        return httpx.Response(200, json=result)

    def _h_unsubscribe(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/iserver/marketdata/(\d+)/unsubscribe$", request.url.path)
        if match is None:
            return httpx.Response(400, json={"error": "invalid path"})
        cid = int(match.group(1))
        self.subscriptions.discard(cid)
        return httpx.Response(200, json={"success": True})

    # ---- Account ----

    def _h_portfolio(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/iserver/account/([^/]+)/portfolio", request.url.path)
        account_id = match.group(1) if match else "UNKNOWN"
        return httpx.Response(
            200,
            json=[
                {"acctId": account_id, "conid": 265598, "position": 10, "avgCost": 145.0, "currency": "USD"},
                {"acctId": account_id, "conid": 272093, "position": 5,  "avgCost": 310.0, "currency": "USD"},
            ],
        )

    def _h_positions(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/iserver/account/([^/]+)/positions", request.url.path)
        account_id = match.group(1) if match else "UNKNOWN"
        return httpx.Response(
            200,
            json=[
                {"acctId": account_id, "conid": 265598, "position": 10, "mktPrice": 150.50, "mktValue": 1505.0, "currency": "USD"},
                {"acctId": account_id, "conid": 272093, "position": 5,  "mktPrice": 320.10, "mktValue": 1600.5, "currency": "USD"},
            ],
        )

    def _h_ledger(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/iserver/account/([^/]+)/ledger", request.url.path)
        account_id = match.group(1) if match else "UNKNOWN"
        return httpx.Response(
            200,
            json={
                "USD": {"acctcode": account_id, "currency": "USD", "cashbalance": 25_000.0, "settledcash": 25_000.0, "key": "LedgerList"},
                "EUR": {"acctcode": account_id, "currency": "EUR", "cashbalance": 5_000.0,  "settledcash": 5_000.0,  "key": "LedgerList"},
            },
        )

    # ---- Orders ----

    def _h_orders_post(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/iserver/account/([^/]+)/orders", request.url.path)
        account_id = match.group(1) if match else "UNKNOWN"
        try:
            body = json.loads(request.content.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        order_specs = body.get("orders") or [body]
        spec = order_specs[0] if order_specs else {}

        # Reply-Confirmation-Loop: wenn reply_warnings konfiguriert ist,
        # liefert der erste Place-Call die Warning-Liste, jeder folgende
        # Reply-Confirm verschiebt eine Warning weiter, bis die echte
        # Order rauskommt.
        if self.reply_warnings:
            warning_ids: list[str] = []
            for warn in self.reply_warnings:
                wid = warn["id"]
                self._pending_replies[wid] = {
                    "spec": spec,
                    "account_id": account_id,
                    "remaining": [w for w in self.reply_warnings if w["id"] != wid],
                }
                warning_ids.append(wid)
                break  # Nur die erste Warning ausgeben - Folgereplies kommen via /iserver/reply/{id}.
            first = self.reply_warnings[0]
            return httpx.Response(
                200,
                json=[
                    {
                        "id": first["id"],
                        "message": list(first.get("message", [])),
                        "isSuppressed": False,
                    }
                ],
            )

        return self._create_real_order(account_id, spec)

    def _create_real_order(self, account_id: str, spec: dict[str, Any]) -> httpx.Response:
        oid = str(self._next_order_id)
        self._next_order_id += 1
        self.orders[oid] = {
            "order_id": oid,
            "account_id": account_id,
            "conid": spec.get("conid"),
            "side": (spec.get("side") or "BUY").upper(),
            "quantity": str(spec.get("quantity", spec.get("qty", 0))),
            "order_type": (spec.get("orderType") or spec.get("order_type") or "MKT").upper(),
            "tif": (spec.get("tif") or "DAY").upper(),
            "limit_price": _to_str_or_none(spec.get("price") or spec.get("limit_price")),
            "stop_price": _to_str_or_none(spec.get("auxPrice") or spec.get("stop_price")),
            "currency": spec.get("currency") or "USD",
            "status": "PendingSubmit",
            "_status_calls": 0,
        }
        return httpx.Response(
            200,
            json=[{"order_id": oid, "order_status": "PendingSubmit", "encrypt_message": "1"}],
        )

    def _h_order_reply(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/iserver/reply/([^/]+)$", request.url.path)
        if match is None:
            return httpx.Response(400, json={"error": "invalid path"})
        reply_id = match.group(1)
        pending = self._pending_replies.pop(reply_id, None)
        if pending is None:
            return httpx.Response(404, json={"error": "unknown reply id"})
        try:
            body = json.loads(request.content.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        if not body.get("confirmed", False):
            return httpx.Response(400, json={"error": "must confirm reply"})

        remaining = pending["remaining"]
        if remaining:
            nxt = remaining[0]
            self._pending_replies[nxt["id"]] = {
                "spec": pending["spec"],
                "account_id": pending["account_id"],
                "remaining": remaining[1:],
            }
            return httpx.Response(
                200,
                json=[
                    {
                        "id": nxt["id"],
                        "message": list(nxt.get("message", [])),
                        "isSuppressed": False,
                    }
                ],
            )
        return self._create_real_order(pending["account_id"], pending["spec"])

    def _h_order_status(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/iserver/account/orders/([^/]+)$", request.url.path)
        if match is None:
            return httpx.Response(400, json={"error": "invalid path"})
        order_id = match.group(1)
        order = self.orders.get(order_id)
        if order is None:
            return httpx.Response(404, json={"error": "unknown order"})
        # Lifecycle: PendingSubmit -> Submitted -> Filled (über aufeinanderfolgende Status-Calls).
        order["_status_calls"] += 1
        if order["status"] == "PendingSubmit":
            order["status"] = "Submitted"
        elif order["status"] == "Submitted":
            order["status"] = "Filled"
        result = {
            "order_id": order_id,
            "order_status": order["status"],
            "conid": order.get("conid"),
            "side": order.get("side"),
            "size": order.get("quantity"),
            "order_type": order.get("order_type"),
            "tif": order.get("tif"),
            "currency": order.get("currency"),
            "limit_price": order.get("limit_price"),
            "stop_price": order.get("stop_price"),
            "account_id": order.get("account_id"),
        }
        if order["status"] == "Filled":
            result["avg_price"] = order.get("limit_price") or "150.00"
            result["filled_quantity"] = order.get("quantity")
            result["commission"] = "1.00"
        return httpx.Response(200, json=result)

    def _h_order_cancel(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/iserver/account/[^/]+/order/([^/]+)$", request.url.path)
        if match is None:
            return httpx.Response(400, json={"error": "invalid path"})
        order_id = match.group(1)
        order = self.orders.get(order_id)
        if order is None:
            return httpx.Response(404, json={"error": "unknown order"})
        order["status"] = "Cancelled"
        return httpx.Response(200, json={"order_id": order_id, "msg": "cancelled"})

    # ---- Trades ----

    def _h_trades(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        days = int(request.url.params.get("days", "1") or "1")
        days = max(1, min(days, 30))
        # Deterministische Mock-Liste: pro Tag ein Trade, Commission 1.50 USD
        # mit explizitem currency-Feld. days bestimmt, wie weit die Liste
        # zurueckreicht.
        # Ankerdatum 2026-04-25 entspricht today im Test - so faellt Trade i=0
        # auf 2026-04-25, i=1 auf 2026-04-24 usw. (immer im April -> selbes
        # Monat, also alle MTD).
        trades: list[dict[str, Any]] = []
        for i in range(days):
            day = max(1, 25 - i)
            entry = {
                "execution_id": f"exec-{i:03d}",
                "order_id": f"ord-{i:03d}",
                "account_id": "U25235077",
                "symbol": "AAPL",
                "conid": 265598,
                "side": "BUY" if i % 2 == 0 else "SELL",
                "size": "1",
                "price": "150.00",
                "net_amount": "150.00",
                "commission": "1.50",
                "currency": "USD",
                "trade_time": f"2026-04-{day:02d} 10:00:00",
            }
            if self.omit_trade_currency:
                entry.pop("currency", None)
            trades.append(entry)
        return httpx.Response(200, json=trades)

    # ---- Registrierung ----

    def register(self, router: respx.Router) -> None:
        b = re.escape(self.base_url)
        router.get(url__regex=rf"^{b}/iserver/auth/status$").mock(side_effect=self._h_auth_status)
        router.post(url__regex=rf"^{b}/tickle$").mock(side_effect=self._h_tickle)
        router.post(url__regex=rf"^{b}/reauthenticate$").mock(side_effect=self._h_reauthenticate)
        router.get(url__regex=rf"^{b}/iserver/secdef/search(\?.*)?$").mock(side_effect=self._h_secdef_search)
        router.get(url__regex=rf"^{b}/iserver/secdef/info(\?.*)?$").mock(side_effect=self._h_secdef_info)
        router.get(url__regex=rf"^{b}/iserver/marketdata/snapshot(\?.*)?$").mock(side_effect=self._h_snapshot)
        router.get(url__regex=rf"^{b}/iserver/marketdata/\d+/unsubscribe$").mock(side_effect=self._h_unsubscribe)
        router.get(url__regex=rf"^{b}/iserver/account/[^/]+/portfolio$").mock(side_effect=self._h_portfolio)
        router.get(url__regex=rf"^{b}/iserver/account/[^/]+/positions$").mock(side_effect=self._h_positions)
        router.get(url__regex=rf"^{b}/iserver/account/[^/]+/ledger$").mock(side_effect=self._h_ledger)
        router.post(url__regex=rf"^{b}/iserver/account/[^/]+/orders$").mock(side_effect=self._h_orders_post)
        router.post(url__regex=rf"^{b}/iserver/reply/[^/]+$").mock(side_effect=self._h_order_reply)
        router.get(url__regex=rf"^{b}/iserver/account/orders/[^/]+$").mock(side_effect=self._h_order_status)
        router.delete(url__regex=rf"^{b}/iserver/account/[^/]+/order/[^/]+$").mock(side_effect=self._h_order_cancel)
        router.get(url__regex=rf"^{b}/iserver/account/trades(\?.*)?$").mock(side_effect=self._h_trades)


@pytest.fixture
def cp_gateway_mock():
    """Liefert ein konfigurierbares MockCPGateway, hängt sich an httpx.

    Standardwert für die Base-URL ist `http://cpgateway:5000/v1/api` -
    das ist der Live-Pfad des IBKR Client Portal Gateway (alle REST-
    Endpunkte liegen unter /v1/api/...). Tests, die eine andere Base-URL
    benötigen, instanziieren MockCPGateway selbst und registrieren es im
    aktiven respx-Router.
    """
    mock = MockCPGateway("http://cpgateway:5000/v1/api")
    with respx.mock(assert_all_called=False) as router:
        mock.register(router)
        yield mock
