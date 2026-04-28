"""ReplayCPGatewayMock: ersetzt die fruehere hartcodierte MockCPGateway.

Statische Endpoints (auth/status, tickle, reauthenticate, secdef-search,
secdef-info, portfolio/U25235077/{summary,positions/pageId,ledger}) werden aus
``tests/fixtures/recorded/`` (live > seed) geladen - ein Recording ist
die einzige Wahrheitsquelle ihrer Bodies. Stateful Endpoints (snapshot
mit Subscription-Refcount + First-Call-Prime, orders mit
Lifecycle/Reply-Confirmation, trades mit ``days``-Schleife, unsubscribe)
generieren ihre Bodies weiterhin im Code, weil sie zur Laufzeit
gepraegte Felder (Order-IDs aus dem Counter, Subscription-Set,
Lifecycle-Stage) traegt - die werden in AP-02 Karte 04 durch
Live-Recordings ergaenzt. Bis dahin behaelt der Mock denselben State,
den er vor dem Refactoring hatte.

Flags (auth_lost, slow_response_ms, pacing_violation_after_n,
reply_warnings, omit_trade_currency) bleiben API-stabil - bestehende
Tests sind die Probe.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from typing import Any

import httpx
import respx

from tests.cp_mock.loader import load_recording


def _to_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


_INSTRUMENTS: dict[str, dict[str, Any]] = {
    "AAPL": {"conid": 265598, "companyName": "APPLE INC",      "currency": "USD", "exchange": "NASDAQ"},
    "MSFT": {"conid": 272093, "companyName": "MICROSOFT CORP", "currency": "USD", "exchange": "NASDAQ"},
    "SAP":  {"conid": 104747, "companyName": "SAP SE",         "currency": "EUR", "exchange": "IBIS"},
}
_BY_CONID: dict[int, dict[str, Any]] = {
    info["conid"]: {"symbol": sym, **info} for sym, info in _INSTRUMENTS.items()
}

_SNAPSHOT_VALUES: dict[int, dict[str, str]] = {
    265598: {"31": "150.50", "84": "150.45", "86": "150.55", "6509": "DPB"},
    272093: {"31": "320.10", "84": "320.05", "86": "320.15", "6509": "DPB"},
    104747: {"31": "120.20", "84": "120.15", "86": "120.25", "6509": "DPB"},
}


class ReplayCPGatewayMock:
    """In-Memory-Mock des IBKR Client Portal Gateways - jetzt mit Recording-
    backed Bodies fuer statische Endpoints.
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
        self.reply_warnings: list[dict[str, Any]] = list(reply_warnings or [])
        self.omit_trade_currency: bool = False
        self._pending_replies: dict[str, dict[str, Any]] = {}

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

    # ---- Recording-Helfer ----

    @staticmethod
    def _recorded(
        endpoint: str,
        *,
        method: str = "GET",
        query: dict[str, str] | None = None,
        call_index: int = 1,
    ) -> tuple[int, Any]:
        """Liefert (status_code, body_json) aus dem passenden Recording."""
        rec = load_recording(
            endpoint, method=method, query=query, call_index=call_index
        )
        return rec.get("status_code", 200), rec.get("body_json")

    # ---- Auth (Recording-backed) ----

    def _h_auth_status(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        status, body = self._recorded("/iserver/auth/status", method="GET")
        body = dict(body or {})
        if self.auth_lost:
            body["authenticated"] = False
            body["fail"] = "session-lost"
        return httpx.Response(status, json=body)

    def _h_tickle(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        status, body = self._recorded("/tickle", method="POST")
        body = json.loads(json.dumps(body))  # tiefe Kopie
        if self.auth_lost and isinstance(body, dict):
            iserver = body.setdefault("iserver", {}).setdefault("authStatus", {})
            iserver["authenticated"] = False
        return httpx.Response(status, json=body)

    def _h_reauthenticate(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        status, body = self._recorded("/reauthenticate", method="POST")
        return httpx.Response(status, json=body)

    # ---- Sec-Def (Recording-backed) ----

    def _h_secdef_search(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        symbol = request.url.params.get("symbol", "").upper()
        if symbol not in _INSTRUMENTS:
            return httpx.Response(200, json=[])
        status, body = self._recorded(
            "/iserver/secdef/search", method="GET", query={"symbol": symbol}
        )
        return httpx.Response(status, json=body)

    def _h_secdef_info(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        try:
            conid = int(request.url.params.get("conid", "0"))
        except ValueError:
            return httpx.Response(400, json={"error": "invalid conid"})
        if conid not in _BY_CONID:
            return httpx.Response(404, json={"error": "unknown conid"})
        status, body = self._recorded(
            "/iserver/secdef/info", method="GET", query={"conid": str(conid)}
        )
        return httpx.Response(status, json=body)

    # ---- Portfolio (Recording-backed, IBKR /portfolio/{aid}/...) ----

    def _h_portfolio_summary(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/portfolio/([^/]+)/summary$", request.url.path)
        account_id = match.group(1) if match else "UNKNOWN"
        status, body = self._recorded(
            f"/portfolio/{account_id}/summary", method="GET"
        )
        return httpx.Response(status, json=body)

    def _h_portfolio_positions(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/portfolio/([^/]+)/positions/([^/?]+)", request.url.path)
        if match is None:
            return httpx.Response(400, json={"error": "invalid path"})
        account_id, page_id = match.group(1), match.group(2)
        try:
            status, body = self._recorded(
                f"/portfolio/{account_id}/positions/{page_id}", method="GET"
            )
            return httpx.Response(status, json=body)
        except LookupError:
            # Pagination-Konvention: nicht aufgezeichnete pageId -> leere Seite.
            return httpx.Response(200, json=[])

    def _h_portfolio_ledger(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        match = re.search(r"/portfolio/([^/]+)/ledger$", request.url.path)
        account_id = match.group(1) if match else "UNKNOWN"
        status, body = self._recorded(
            f"/portfolio/{account_id}/ledger", method="GET"
        )
        return httpx.Response(status, json=body)

    # ---- Marktdaten (stateful, Code-generiert) ----

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

    # ---- Orders (stateful, Code-generiert) ----

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
                break
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
        match = re.search(
            r"/iserver/account/order/status/([^/]+)$", request.url.path
        )
        if match is None:
            return httpx.Response(400, json={"error": "invalid path"})
        order_id = match.group(1)
        order = self.orders.get(order_id)
        if order is None:
            return httpx.Response(404, json={"error": "unknown order"})
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

    # ---- Trades (stateful, Code-generiert) ----

    def _h_trades(self, request: httpx.Request) -> httpx.Response:
        if (resp := self._pre()) is not None:
            return resp
        days = int(request.url.params.get("days", "1") or "1")
        days = max(1, min(days, 30))
        trades: list[dict[str, Any]] = []
        for i in range(days):
            day = max(1, 25 - i)
            # IBKR-Live-Schema (Recording AP-02 #04): `account` statt
            # `account_id`, `listing_exchange` statt direkter `currency`.
            entry = {
                "execution_id": f"exec-{i:03d}",
                "order_id": f"ord-{i:03d}",
                "account": "U25235077",
                "accountCode": "U25235077",
                "symbol": "AAPL",
                "conid": 265598,
                "side": "BUY" if i % 2 == 0 else "SELL",
                "size": "1",
                "price": "150.00",
                "net_amount": "150.00",
                "commission": "1.50",
                "listing_exchange": "NASDAQ",
                "trade_time": f"2026-04-{day:02d} 10:00:00",
            }
            if self.omit_trade_currency:
                # Sowohl Legacy-Currency als auch Exchange entfernen,
                # damit der Adapter auf den Fallback-Pfad (USD-Annahme +
                # currency_assumed=True) faellt.
                entry.pop("currency", None)
                entry.pop("listing_exchange", None)
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
        router.get(url__regex=rf"^{b}/portfolio/[^/]+/summary$").mock(side_effect=self._h_portfolio_summary)
        router.get(url__regex=rf"^{b}/portfolio/[^/]+/positions/[^/]+$").mock(side_effect=self._h_portfolio_positions)
        router.get(url__regex=rf"^{b}/portfolio/[^/]+/ledger$").mock(side_effect=self._h_portfolio_ledger)
        router.post(url__regex=rf"^{b}/iserver/account/[^/]+/orders$").mock(side_effect=self._h_orders_post)
        router.post(url__regex=rf"^{b}/iserver/reply/[^/]+$").mock(side_effect=self._h_order_reply)
        router.get(url__regex=rf"^{b}/iserver/account/order/status/[^/]+$").mock(side_effect=self._h_order_status)
        router.delete(url__regex=rf"^{b}/iserver/account/[^/]+/order/[^/]+$").mock(side_effect=self._h_order_cancel)
        router.get(url__regex=rf"^{b}/iserver/account/trades(\?.*)?$").mock(side_effect=self._h_trades)
