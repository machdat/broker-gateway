"""Unit-Tests fuer tests_paper/_dsl/actions.py (AP-07 K5).

Laufen in der Default-tests/-Suite gegen einen schlanken Fake-
HTTP-Client; kein Paper-Stack noetig.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tests_paper._dsl.actions import (
    cancel_all_open_orders,
    cancel_order,
    place_limit_far_from_market,
    subscribe_quote_stream,
    wait_for_order_status,
)
from tests_paper._dsl.safety import PaperSafetyError


# ---------------------------------------------------------------------------
# Fake HTTP-Client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self, status_code: int, body: Any, *, headers: dict | None = None
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self) -> Any:
        return self._body

    @property
    def text(self) -> str:
        """Body als String - wie ``httpx.Response.text``."""
        return str(self._body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self  # type: ignore[arg-type]
            )


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.routes: dict[tuple[str, str], _FakeResponse] = {}

    def route(
        self,
        method: str,
        path: str,
        response: _FakeResponse,
    ) -> None:
        self.routes[(method.upper(), path)] = response

    async def get(self, path: str, *, params: dict | None = None) -> _FakeResponse:
        self.calls.append(("GET", path, {"params": params or {}}))
        return self.routes.get(("GET", path), _FakeResponse(404, {}))

    async def post(
        self, path: str, *, json: Any = None, headers: dict | None = None
    ) -> _FakeResponse:
        self.calls.append(
            ("POST", path, {"json": json, "headers": headers or {}})
        )
        return self.routes.get(("POST", path), _FakeResponse(404, {}))

    async def delete(
        self, path: str, *, headers: dict | None = None
    ) -> _FakeResponse:
        self.calls.append(("DELETE", path, {"headers": headers or {}}))
        return self.routes.get(("DELETE", path), _FakeResponse(204, {}))


# ---------------------------------------------------------------------------
# place_limit_far_from_market
# ---------------------------------------------------------------------------


async def test_place_limit_far_from_market_happy_path_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BG_PAPER_TESTS_DISABLED", raising=False)
    client = _FakeClient()
    client.route(
        "GET",
        "/v1/quotes/snapshot",
        _FakeResponse(200, [{"conid": 100, "last": "100.00"}]),
    )
    client.route("POST", "/v1/orders", _FakeResponse(201, {"order_id": 999}))

    order_id = await place_limit_far_from_market(
        client, "DU1234567", 100, "BUY", distance_pct=20, qty=1
    )

    assert order_id == 999
    # Limit-Preis: 100 * (100-20)/100 = 80.00
    post_call = next(c for c in client.calls if c[0] == "POST")
    assert post_call[2]["json"]["limit_price"] == "80.00"
    assert post_call[2]["json"]["side"] == "BUY"


async def test_place_limit_far_from_market_sell_inverts_distance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BG_PAPER_TESTS_DISABLED", raising=False)
    client = _FakeClient()
    client.route(
        "GET",
        "/v1/quotes/snapshot",
        _FakeResponse(200, [{"conid": 100, "last": "100.00"}]),
    )
    client.route("POST", "/v1/orders", _FakeResponse(201, {"order_id": 1000}))

    await place_limit_far_from_market(
        client, "DU1234567", 100, "SELL", distance_pct=20
    )
    post_call = next(c for c in client.calls if c[0] == "POST")
    # SELL: 100 * 1.20 = 120.00
    assert post_call[2]["json"]["limit_price"] == "120.00"


async def test_place_limit_below_min_distance_rejected() -> None:
    client = _FakeClient()
    with pytest.raises(PaperSafetyError, match="Minimum"):
        await place_limit_far_from_market(
            client, "DU1234567", 100, "BUY", distance_pct=2
        )


async def test_place_limit_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BG_PAPER_TESTS_DISABLED", "true")
    with pytest.raises(PaperSafetyError, match="DISABLED"):
        await place_limit_far_from_market(
            _FakeClient(), "DU1234567", 100, "BUY"
        )


async def test_place_limit_rejects_live_account() -> None:
    with pytest.raises(PaperSafetyError, match="DU-Praefix"):
        await place_limit_far_from_market(
            _FakeClient(), "U25235077", 100, "BUY"
        )


# ---------------------------------------------------------------------------
# cancel_order / cancel_all_open_orders
# ---------------------------------------------------------------------------


async def test_cancel_order_passes_idempotency_header() -> None:
    client = _FakeClient()
    client.route(
        "DELETE", "/v1/orders/999", _FakeResponse(204, {})
    )
    await cancel_order(client, "DU1234567", 999, idempotency_key="key-1")
    delete_call = next(c for c in client.calls if c[0] == "DELETE")
    assert delete_call[2]["headers"]["Idempotency-Key"] == "key-1"


async def test_cancel_all_open_orders_idempotent_on_empty() -> None:
    client = _FakeClient()
    client.route(
        "GET", "/v1/orders", _FakeResponse(200, {"orders": []})
    )
    cancelled = await cancel_all_open_orders(client, "DU1234567")
    assert cancelled == []
    # Zweiter Aufruf - kein Crash, weiterhin leer.
    cancelled2 = await cancel_all_open_orders(client, "DU1234567")
    assert cancelled2 == []


async def test_cancel_all_open_orders_deletes_each_order() -> None:
    client = _FakeClient()
    client.route(
        "GET",
        "/v1/orders",
        _FakeResponse(200, [{"order_id": 1}, {"order_id": 2}]),
    )
    client.route("DELETE", "/v1/orders/1", _FakeResponse(204, {}))
    client.route("DELETE", "/v1/orders/2", _FakeResponse(204, {}))

    cancelled = await cancel_all_open_orders(client, "DU1234567")
    assert sorted(cancelled) == [1, 2]
    deletes = [c for c in client.calls if c[0] == "DELETE"]
    assert {c[1] for c in deletes} == {"/v1/orders/1", "/v1/orders/2"}


async def test_cancel_all_open_orders_sends_idempotency_key() -> None:
    """Ohne Idempotency-Key lehnt der Service den DELETE mit 400 ab.

    Karte f42eb6cf: der Cleanup schickte den Pflicht-Header nicht, der
    Cancel erreichte IBKR nie und die Order blieb offen stehen.
    """
    client = _FakeClient()
    client.route(
        "GET",
        "/v1/orders",
        _FakeResponse(200, [{"order_id": 1}, {"order_id": 2}]),
    )
    client.route("DELETE", "/v1/orders/1", _FakeResponse(204, {}))
    client.route("DELETE", "/v1/orders/2", _FakeResponse(204, {}))

    await cancel_all_open_orders(client, "DU1234567")

    keys = [
        c[2]["headers"].get("Idempotency-Key")
        for c in client.calls
        if c[0] == "DELETE"
    ]
    assert len(keys) == 2
    assert all(keys), "jeder Cancel braucht einen Idempotency-Key"
    assert len(set(keys)) == 2, "je Order ein eigener Key"


async def test_cancel_all_open_orders_skips_failed_cancel() -> None:
    """Nicht-2xx zählt nicht als storniert, warnt aber sichtbar."""
    client = _FakeClient()
    client.route(
        "GET", "/v1/orders", _FakeResponse(200, [{"order_id": 7}])
    )
    client.route(
        "DELETE",
        "/v1/orders/7",
        _FakeResponse(400, {"error": "invalid_input"}),
    )

    with pytest.warns(UserWarning, match="400"):
        cancelled = await cancel_all_open_orders(client, "DU1234567")

    assert cancelled == []


# ---------------------------------------------------------------------------
# subscribe_quote_stream
# ---------------------------------------------------------------------------


async def test_subscribe_quote_stream_rejects_empty_fields() -> None:
    with pytest.raises(RuntimeError, match="fields"):
        async with subscribe_quote_stream(
            _FakeClient(), [100], fields=[]  # type: ignore[arg-type]
        ):  # noqa: SIM117
            pass  # pragma: no cover


async def test_subscribe_quote_stream_rejects_empty_conids() -> None:
    class _StreamClient:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):  # pragma: no cover
            yield None

    with pytest.raises(RuntimeError, match="conids"):
        async with subscribe_quote_stream(
            _StreamClient(), [], fields=["last"]  # type: ignore[arg-type]
        ):  # noqa: SIM117
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# wait_for_order_status
# ---------------------------------------------------------------------------


async def test_wait_for_order_status_returns_immediately_on_match() -> None:
    client = _FakeClient()
    client.route(
        "GET",
        "/v1/orders/999",
        _FakeResponse(200, {"order_id": 999, "status": "Filled"}),
    )
    status = await wait_for_order_status(
        client,
        "DU1234567",
        999,
        target="filled",
        timeout_s=1.0,
        poll_interval_s=0.01,
    )
    assert status == "Filled"


async def test_wait_for_order_status_timeout() -> None:
    client = _FakeClient()
    client.route(
        "GET",
        "/v1/orders/999",
        _FakeResponse(200, {"order_id": 999, "status": "PendingSubmit"}),
    )
    with pytest.raises(TimeoutError):
        await wait_for_order_status(
            client,
            "DU1234567",
            999,
            target=("filled", "cancelled"),
            timeout_s=0.05,
            poll_interval_s=0.01,
        )


async def test_wait_for_order_status_accepts_target_iterable() -> None:
    client = _FakeClient()
    client.route(
        "GET",
        "/v1/orders/999",
        _FakeResponse(200, {"order_id": 999, "status": "cancelled"}),
    )
    status = await wait_for_order_status(
        client,
        "DU1234567",
        999,
        target=["filled", "cancelled"],
        timeout_s=1.0,
        poll_interval_s=0.01,
    )
    assert status == "cancelled"
