"""Tests fuer broker_gateway.tws.orders (Karte 064fa82d, Phase 4).

Coverage-Ziel: >=90% fuer src/broker_gateway/tws/orders.py.

Mock-Strategie: TWSClient.``_ib`` wird durch einen SimpleNamespace
ersetzt, der die ib_async-Methoden ``placeOrder``, ``cancelOrder``,
``openTrades``, ``trades``, ``managedAccounts`` und
``qualifyContractsAsync`` simuliert.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from broker_gateway.cp.topics.sor import SorFrame
from broker_gateway.order_models import (
    OrderModifyRequest,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    WhatIfPreview,
)
from broker_gateway.streams.orders import OrdersBroadcaster
from broker_gateway.tws.orders import (
    TWSOrdersBootstrapLoader,
    TWSOrdersService,
    TWSOrdersStreamPump,
    _account_base_currency,
    _build_ib_order,
    _decimal_str,
    _first_finite,
    _is_finite_number,
    _order_state_to_whatif,
    _parse_whatif_warning,
    _to_decimal,
    _trade_to_order,
    _trade_to_sor_frame,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_contract(*, conid: int = 265598, symbol: str = "AAPL", currency: str = "USD") -> SimpleNamespace:
    return SimpleNamespace(conId=conid, symbol=symbol, currency=currency, exchange="SMART", secType="STK")


def _make_order_obj(
    *,
    perm_id: int = 0,
    order_id: int = 0,
    action: str = "BUY",
    order_type: str = "LMT",
    tif: str = "DAY",
    total_quantity: float = 10.0,
    lmt_price: float | None = 200.0,
    aux_price: float | None = None,
    account: str = "U25235077",
    parent_id: int = 0,
    order_ref: str | None = None,
    oca_group: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        permId=perm_id,
        orderId=order_id,
        action=action,
        orderType=order_type,
        tif=tif,
        totalQuantity=total_quantity,
        lmtPrice=lmt_price,
        auxPrice=aux_price,
        account=account,
        parentId=parent_id,
        orderRef=order_ref,
        ocaGroup=oca_group,
    )


def _make_order_status(
    *,
    status: str = "Submitted",
    filled: float = 0.0,
    avg_fill_price: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(status=status, filled=filled, avgFillPrice=avg_fill_price)


def _make_trade(
    *,
    perm_id: int = 0,
    order_id: int = 0,
    status: str = "Submitted",
    contract: SimpleNamespace | None = None,
    account: str = "U25235077",
    total_quantity: float = 10.0,
    lmt_price: float | None = 200.0,
    filled: float = 0.0,
    avg_fill_price: float = 0.0,
    fills: list[SimpleNamespace] | None = None,
    log: list[SimpleNamespace] | None = None,
    order: SimpleNamespace | None = None,
) -> SimpleNamespace:
    if contract is None:
        contract = _make_contract()
    return SimpleNamespace(
        order=order
        if order is not None
        else _make_order_obj(
            perm_id=perm_id,
            order_id=order_id,
            account=account,
            total_quantity=total_quantity,
            lmt_price=lmt_price,
        ),
        orderStatus=_make_order_status(
            status=status, filled=filled, avg_fill_price=avg_fill_price
        ),
        contract=contract,
        fills=fills if fills is not None else [],
        log=log if log is not None else [],
    )


def _make_fill(*, commission: float, currency: str = "USD") -> SimpleNamespace:
    return SimpleNamespace(
        commissionReport=SimpleNamespace(commission=commission, currency=currency),
    )


def _make_client(
    *,
    managed_accounts: list[str] | None = None,
    open_trades: list[SimpleNamespace] | None = None,
    trades: list[SimpleNamespace] | None = None,
    qualify_results: list[Any] | None = None,
    place_side_effect: Any = None,
    cancel_side_effect: Any = None,
) -> MagicMock:
    qualified_default = [_make_contract()]
    ib = SimpleNamespace(
        managedAccounts=MagicMock(
            return_value=list(managed_accounts) if managed_accounts is not None else ["U25235077"]
        ),
        openTrades=MagicMock(return_value=list(open_trades or [])),
        trades=MagicMock(return_value=list(trades or open_trades or [])),
        qualifyContractsAsync=AsyncMock(
            return_value=qualify_results if qualify_results is not None else qualified_default
        ),
        placeOrder=MagicMock(
            side_effect=place_side_effect,
            return_value=None if place_side_effect else _make_trade(perm_id=999, order_id=42),
        ),
        cancelOrder=MagicMock(side_effect=cancel_side_effect, return_value=None),
    )
    client = MagicMock()
    client._ib = ib
    client.read_only = False
    return client


def _order_request(**overrides: Any) -> OrderRequest:
    payload: dict[str, Any] = {
        "account_id": "U25235077",
        "conid": 265598,
        "side": OrderSide.BUY,
        "quantity": "10",
        "order_type": OrderType.LMT,
        "tif": TimeInForce.DAY,
        "limit_price": "200.50",
    }
    payload.update(overrides)
    return OrderRequest(**payload)


# --------------------------------------------------------------------------
# place_order
# --------------------------------------------------------------------------


class TestPlaceOrder:
    async def test_503_in_read_only(self) -> None:
        client = _make_client()
        service = TWSOrdersService(client, read_only=True)
        with pytest.raises(HTTPException) as exc_info:
            await service.place_order(_order_request())
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["code"] == "read_only_api"

    async def test_pulls_read_only_from_client_default(self) -> None:
        client = _make_client()
        client.read_only = True
        service = TWSOrdersService(client)
        with pytest.raises(HTTPException) as exc_info:
            await service.place_order(_order_request())
        assert exc_info.value.status_code == 503

    async def test_validates_account(self) -> None:
        client = _make_client(managed_accounts=["DUP799747"])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.place_order(_order_request())
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "invalid_account"

    async def test_invalid_conid_when_qualify_empty(self) -> None:
        client = _make_client(qualify_results=[])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.place_order(_order_request())
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "invalid_conid"

    async def test_returns_order_after_place(self) -> None:
        placed_trade = _make_trade(
            perm_id=12345, order_id=10, status="Submitted",
            account="U25235077", total_quantity=10.0, lmt_price=200.5,
        )
        client = _make_client()
        client._ib.placeOrder.return_value = placed_trade
        service = TWSOrdersService(client, read_only=False)
        result = await service.place_order(_order_request())
        assert result.order_id == "12345"
        assert result.account_id == "U25235077"
        assert result.side == OrderSide.BUY
        assert result.order_type == OrderType.LMT
        assert result.tif == TimeInForce.DAY
        assert result.status == OrderStatus.SUBMITTED
        assert result.limit_price == "200.5"
        client._ib.placeOrder.assert_called_once()

    async def test_falls_back_to_order_id_when_perm_id_zero(self) -> None:
        placed_trade = _make_trade(perm_id=0, order_id=42)
        client = _make_client()
        client._ib.placeOrder.return_value = placed_trade
        service = TWSOrdersService(client, read_only=False)
        result = await service.place_order(_order_request())
        assert result.order_id == "42"

    async def test_propagates_place_error_as_502(self) -> None:
        client = _make_client(place_side_effect=RuntimeError("boom"))
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.place_order(_order_request())
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["code"] == "tws_place_failed"

    async def test_skips_account_validation_when_managed_accounts_empty(self) -> None:
        client = _make_client(managed_accounts=[])
        client._ib.placeOrder.return_value = _make_trade(perm_id=1)
        service = TWSOrdersService(client, read_only=False)
        result = await service.place_order(_order_request())
        assert result.order_id == "1"

    async def test_qualify_returns_nested_list(self) -> None:
        nested = [[_make_contract()]]
        client = _make_client(qualify_results=nested)
        client._ib.placeOrder.return_value = _make_trade(perm_id=1)
        service = TWSOrdersService(client, read_only=False)
        result = await service.place_order(_order_request())
        assert result.order_id == "1"

    async def test_qualify_handles_nested_empty_list(self) -> None:
        nested = [[]]
        client = _make_client(qualify_results=nested)
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.place_order(_order_request())
        assert exc_info.value.status_code == 400

    async def test_qualify_exception_returns_400(self) -> None:
        client = _make_client()
        client._ib.qualifyContractsAsync = AsyncMock(side_effect=RuntimeError("conid net"))
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.place_order(_order_request())
        assert exc_info.value.status_code == 400


# --------------------------------------------------------------------------
# cancel_order
# --------------------------------------------------------------------------


class TestCancelOrder:
    async def test_cancels_open_trade_by_perm_id(self) -> None:
        trade = _make_trade(perm_id=999, order_id=10, account="U25235077")
        client = _make_client(open_trades=[trade], trades=[trade])
        service = TWSOrdersService(client, read_only=False)
        result = await service.cancel_order("U25235077", "999")
        assert result.order_id == "999"
        client._ib.cancelOrder.assert_called_once()

    async def test_cancels_by_order_id_when_perm_id_zero(self) -> None:
        trade = _make_trade(perm_id=0, order_id=42, account="U25235077")
        client = _make_client(open_trades=[trade], trades=[trade])
        service = TWSOrdersService(client, read_only=False)
        result = await service.cancel_order("U25235077", "42")
        assert result.order_id == "42"

    async def test_404_when_unknown(self) -> None:
        client = _make_client(open_trades=[], trades=[])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.cancel_order("U25235077", "999")
        assert exc_info.value.status_code == 404

    async def test_404_when_account_does_not_match(self) -> None:
        trade = _make_trade(perm_id=999, account="DUP799747")
        client = _make_client(trades=[trade])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.cancel_order("U25235077", "999")
        assert exc_info.value.status_code == 404

    async def test_propagates_cancel_error_as_502(self) -> None:
        trade = _make_trade(perm_id=999, account="U25235077")
        client = _make_client(
            trades=[trade], cancel_side_effect=RuntimeError("ibkr down")
        )
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.cancel_order("U25235077", "999")
        assert exc_info.value.status_code == 502

    async def test_invalid_order_id_returns_404(self) -> None:
        client = _make_client(trades=[_make_trade(perm_id=999)])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.cancel_order("U25235077", "not-an-int")
        assert exc_info.value.status_code == 404


# --------------------------------------------------------------------------
# modify_order (Karte 35ac9a17)
# --------------------------------------------------------------------------


def _modify(**overrides: Any) -> OrderModifyRequest:
    payload: dict[str, Any] = {"account_id": "U25235077", "stop_price": "195.50"}
    payload.update(overrides)
    return OrderModifyRequest(**payload)


class TestModifyOrder:
    async def test_503_in_read_only(self) -> None:
        client = _make_client()
        service = TWSOrdersService(client, read_only=True)
        with pytest.raises(HTTPException) as exc_info:
            await service.modify_order("U25235077", "999", _modify())
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["code"] == "read_only_api"

    async def test_404_when_unknown(self) -> None:
        client = _make_client(trades=[])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.modify_order("U25235077", "999", _modify())
        assert exc_info.value.status_code == 404

    async def test_404_when_account_mismatch(self) -> None:
        trade = _make_trade(perm_id=999, account="DUP799747")
        client = _make_client(trades=[trade])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.modify_order("U25235077", "999", _modify())
        assert exc_info.value.status_code == 404

    async def test_applies_stop_price_and_replaces_via_placeorder(self) -> None:
        # GTC-STP-Order, Stop von 180 auf 195.50 nach oben modifizieren.
        trade = _make_trade(perm_id=999, order_id=10, account="U25235077")
        trade.order.orderType = "STP"
        trade.order.tif = "GTC"
        trade.order.auxPrice = 180.0
        client = _make_client(open_trades=[trade], trades=[trade])
        client._ib.placeOrder.return_value = trade
        service = TWSOrdersService(client, read_only=False)
        result = await service.modify_order("U25235077", "999", _modify(stop_price="195.50"))
        # Das bestehende Order-Objekt wurde modifiziert (cancel/replace via
        # gleiche orderId) und placeOrder erneut gerufen.
        assert trade.order.auxPrice == 195.5
        client._ib.placeOrder.assert_called_once()
        assert result.order_id == "999"

    async def test_applies_limit_and_quantity(self) -> None:
        trade = _make_trade(perm_id=5, order_id=7, account="U25235077")
        trade.order.orderType = "STP LMT"
        client = _make_client(trades=[trade])
        client._ib.placeOrder.return_value = trade
        service = TWSOrdersService(client, read_only=False)
        await service.modify_order(
            "U25235077", "5", _modify(stop_price="100", limit_price="99.5", quantity="3")
        )
        assert trade.order.auxPrice == 100.0
        assert trade.order.lmtPrice == 99.5
        assert trade.order.totalQuantity == 3.0

    async def test_propagates_modify_error_as_502(self) -> None:
        trade = _make_trade(perm_id=999, account="U25235077")
        client = _make_client(trades=[trade], place_side_effect=RuntimeError("boom"))
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.modify_order("U25235077", "999", _modify())
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["code"] == "tws_modify_failed"

    async def test_502_when_trade_has_no_contract(self) -> None:
        # _find_trade matcht ueber order.permId/orderId; ein order=None-Trade
        # wuerde uebersprungen (-> 404). Fuer den 502-Pfad brauchen wir ein
        # matchbares Order-Objekt, dessen contract fehlt.
        order_obj = _make_order_obj(perm_id=999, account="U25235077")
        partial = SimpleNamespace(
            order=order_obj,
            orderStatus=_make_order_status(),
            contract=None,
            fills=[],
            log=[],
        )
        client = _make_client(trades=[partial])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.modify_order("U25235077", "999", _modify())
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["code"] == "tws_modify_no_order"


# --------------------------------------------------------------------------
# get_order / list_open
# --------------------------------------------------------------------------


class TestGetOrder:
    async def test_finds_by_perm_id(self) -> None:
        trade = _make_trade(perm_id=999, order_id=10, status="Filled", filled=10.0)
        client = _make_client(trades=[trade])
        service = TWSOrdersService(client, read_only=False)
        result = await service.get_order("999")
        assert result.order_id == "999"
        assert result.status == OrderStatus.FILLED

    async def test_finds_by_order_id(self) -> None:
        trade = _make_trade(perm_id=0, order_id=42, status="Submitted")
        client = _make_client(trades=[trade])
        service = TWSOrdersService(client, read_only=False)
        result = await service.get_order("42")
        assert result.order_id == "42"

    async def test_404_when_unknown(self) -> None:
        client = _make_client(trades=[])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.get_order("777")
        assert exc_info.value.status_code == 404


class TestListOpen:
    async def test_returns_all_open_trades(self) -> None:
        trades = [
            _make_trade(perm_id=1, account="U25235077"),
            _make_trade(perm_id=2, account="DUP799747"),
        ]
        client = _make_client(open_trades=trades)
        service = TWSOrdersService(client, read_only=False)
        result = await service.list_open()
        ids = sorted(o.order_id for o in result)
        assert ids == ["1", "2"]

    async def test_empty_when_no_open_trades(self) -> None:
        client = _make_client(open_trades=[])
        service = TWSOrdersService(client, read_only=False)
        assert await service.list_open() == []

    async def test_falls_back_to_trades_when_no_open_trades_method(self) -> None:
        ib = SimpleNamespace(
            trades=MagicMock(return_value=[_make_trade(perm_id=5)]),
        )
        client = MagicMock()
        client._ib = ib
        client.read_only = False
        service = TWSOrdersService(client, read_only=False)
        result = await service.list_open()
        assert len(result) == 1
        assert result[0].order_id == "5"


# --------------------------------------------------------------------------
# TWSOrdersBootstrapLoader
# --------------------------------------------------------------------------


class TestBootstrapLoader:
    async def test_load_returns_sor_frames(self) -> None:
        trades = [
            _make_trade(perm_id=11, account="U25235077", status="Submitted"),
            _make_trade(perm_id=22, account="U25235077", status="Filled", filled=5.0),
        ]
        client = _make_client(open_trades=trades)
        loader = TWSOrdersBootstrapLoader(client)
        frames = await loader.load("U25235077")
        ids = sorted(f.order_id for f in frames)
        assert ids == [11, 22]
        statuses = sorted(f.status for f in frames)
        assert "filled" in statuses
        assert "accepted" in statuses

    async def test_load_filters_by_account(self) -> None:
        trades = [
            _make_trade(perm_id=11, account="U25235077"),
            _make_trade(perm_id=22, account="DUP799747"),
        ]
        client = _make_client(open_trades=trades)
        loader = TWSOrdersBootstrapLoader(client)
        frames = await loader.load("U25235077")
        assert len(frames) == 1
        assert frames[0].order_id == 11

    async def test_load_skips_trades_without_perm_or_order_id(self) -> None:
        trade = _make_trade(perm_id=0, order_id=0, account="U25235077")
        client = _make_client(open_trades=[trade])
        loader = TWSOrdersBootstrapLoader(client)
        frames = await loader.load("U25235077")
        assert frames == []


# --------------------------------------------------------------------------
# TWSOrdersStreamPump
# --------------------------------------------------------------------------


class _Event:
    """Minimaler Stub fuer ein ib_async-Event-Objekt mit ``+=`` / ``-=``."""

    def __init__(self) -> None:
        self.callbacks: list[Any] = []

    def __iadd__(self, callback: Any) -> "_Event":
        self.callbacks.append(callback)
        return self

    def __isub__(self, callback: Any) -> "_Event":
        try:
            self.callbacks.remove(callback)
        except ValueError:
            pass
        return self

    def emit(self, *args: Any) -> None:
        for cb in list(self.callbacks):
            cb(*args)


def _make_pump_client() -> tuple[MagicMock, _Event, _Event, _Event]:
    open_event = _Event()
    status_event = _Event()
    exec_event = _Event()
    ib = SimpleNamespace(
        openOrderEvent=open_event,
        orderStatusEvent=status_event,
        execDetailsEvent=exec_event,
    )
    client = MagicMock()
    client._ib = ib
    return client, open_event, status_event, exec_event


class TestStreamPump:
    def test_start_attaches_listeners(self) -> None:
        client, open_event, status_event, exec_event = _make_pump_client()
        broadcaster = OrdersBroadcaster()
        pump = TWSOrdersStreamPump(client, broadcaster)
        pump.start()
        assert pump.attached
        assert len(open_event.callbacks) == 1
        assert len(status_event.callbacks) == 1
        assert len(exec_event.callbacks) == 1

    def test_start_idempotent(self) -> None:
        client, open_event, _, _ = _make_pump_client()
        broadcaster = OrdersBroadcaster()
        pump = TWSOrdersStreamPump(client, broadcaster)
        pump.start()
        pump.start()
        assert len(open_event.callbacks) == 1

    def test_stop_detaches_listeners(self) -> None:
        client, open_event, status_event, exec_event = _make_pump_client()
        broadcaster = OrdersBroadcaster()
        pump = TWSOrdersStreamPump(client, broadcaster)
        pump.start()
        pump.stop()
        assert not pump.attached
        assert open_event.callbacks == []
        assert status_event.callbacks == []
        assert exec_event.callbacks == []

    def test_stop_without_start_is_noop(self) -> None:
        client, _, _, _ = _make_pump_client()
        broadcaster = OrdersBroadcaster()
        pump = TWSOrdersStreamPump(client, broadcaster)
        pump.stop()
        assert not pump.attached

    def test_publishes_to_broadcaster_on_status_event(self) -> None:
        client, _, status_event, _ = _make_pump_client()
        broadcaster = OrdersBroadcaster()
        pump = TWSOrdersStreamPump(client, broadcaster)
        pump.start()
        trade = _make_trade(perm_id=999, account="U25235077", status="Filled")
        # Account fuer broadcaster muss existieren - publish ist no-op,
        # wenn keiner subscribed ist. Wir pruefen indirekt ueber den
        # Broadcaster-Snapshot.
        published: list[Any] = []
        original_publish = broadcaster.publish

        def _capture(account: str, frame: Any) -> None:
            published.append((account, frame))
            original_publish(account, frame)

        broadcaster.publish = _capture  # type: ignore[method-assign]
        status_event.emit(trade)
        assert len(published) == 1
        assert published[0][0] == "U25235077"
        assert isinstance(published[0][1], SorFrame)

    def test_publishes_on_exec_event(self) -> None:
        client, _, _, exec_event = _make_pump_client()
        broadcaster = OrdersBroadcaster()
        pump = TWSOrdersStreamPump(client, broadcaster)
        pump.start()
        trade = _make_trade(perm_id=42, account="U25235077")
        captured: list[Any] = []
        broadcaster.publish = lambda *args: captured.append(args)  # type: ignore[method-assign]
        exec_event.emit(trade, SimpleNamespace())
        assert len(captured) == 1

    def test_skip_publish_when_no_account(self) -> None:
        client, _, status_event, _ = _make_pump_client()
        broadcaster = OrdersBroadcaster()
        pump = TWSOrdersStreamPump(client, broadcaster)
        pump.start()
        trade = _make_trade(perm_id=42, account="")
        captured: list[Any] = []
        broadcaster.publish = lambda *args: captured.append(args)  # type: ignore[method-assign]
        status_event.emit(trade)
        assert captured == []

    def test_skip_publish_when_no_perm_or_order_id(self) -> None:
        client, _, status_event, _ = _make_pump_client()
        broadcaster = OrdersBroadcaster()
        pump = TWSOrdersStreamPump(client, broadcaster)
        pump.start()
        trade = _make_trade(perm_id=0, order_id=0, account="U25235077")
        captured: list[Any] = []
        broadcaster.publish = lambda *args: captured.append(args)  # type: ignore[method-assign]
        status_event.emit(trade)
        assert captured == []

    def test_publish_failure_does_not_propagate(self) -> None:
        client, _, status_event, _ = _make_pump_client()
        broadcaster = OrdersBroadcaster()
        pump = TWSOrdersStreamPump(client, broadcaster)
        pump.start()
        trade = _make_trade(perm_id=42, account="U25235077")

        def _boom(*args: Any) -> None:
            raise RuntimeError("broadcaster broken")

        broadcaster.publish = _boom  # type: ignore[method-assign]
        # darf nicht aus dem Listener herauseskalieren
        status_event.emit(trade)


# --------------------------------------------------------------------------
# Mapping helpers
# --------------------------------------------------------------------------


class TestTradeToOrder:
    def test_full_trade_maps_all_fields(self) -> None:
        fills = [_make_fill(commission=0.5), _make_fill(commission=0.7)]
        trade = _make_trade(
            perm_id=123, order_id=10, status="Filled",
            account="U25235077", total_quantity=10.0, lmt_price=200.5,
            filled=10.0, avg_fill_price=200.6, fills=fills,
        )
        order = _trade_to_order(trade)
        assert order.order_id == "123"
        assert order.account_id == "U25235077"
        assert order.conid == 265598
        assert order.side == OrderSide.BUY
        assert order.order_type == OrderType.LMT
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity is not None
        assert Decimal(order.filled_quantity) == Decimal("10")
        assert order.avg_fill_price is not None
        assert order.avg_fill_price.value == "200.6"
        assert order.commission is not None
        assert order.commission.value == "1.2"
        assert order.commission.currency == "USD"

    def test_unknown_status_falls_back_to_submitted(self) -> None:
        trade = _make_trade(perm_id=1, status="Weirdo")
        order = _trade_to_order(trade)
        assert order.status == OrderStatus.SUBMITTED

    def test_request_provides_fallback_for_missing_fields(self) -> None:
        # Ein Order-Stub ohne action/orderType/tif → Werte aus request
        order_obj = SimpleNamespace(
            permId=0, orderId=42, action=None, orderType=None, tif=None,
            totalQuantity=None, lmtPrice=None, auxPrice=None,
            account=None, parentId=0, orderRef=None,
        )
        trade = SimpleNamespace(
            order=order_obj,
            orderStatus=_make_order_status(status="Submitted"),
            contract=_make_contract(),
            fills=[], log=[],
        )
        request = _order_request(quantity="7")
        result = _trade_to_order(trade, request=request)
        assert result.order_id == "42"
        assert result.account_id == request.account_id
        assert result.quantity == "7"
        assert result.limit_price == request.limit_price

    def test_stp_lmt_order_type_normalised(self) -> None:
        trade = _make_trade(perm_id=1)
        trade.order.orderType = "STP LMT"
        order = _trade_to_order(trade)
        assert order.order_type == OrderType.STP_LMT

    def test_oca_group_mapped(self) -> None:
        # Karte def3e8f5: OCA-Gruppe wird durchgereicht (Stop-/Bracket-Sicht).
        trade = _make_trade(perm_id=1)
        trade.order.ocaGroup = "bracket-7"
        order = _trade_to_order(trade)
        assert order.oca_group == "bracket-7"

    def test_empty_oca_group_becomes_none(self) -> None:
        # ib_async-Default ocaGroup="" -> None (keine OCA-Gruppe).
        trade = _make_trade(perm_id=1)
        order = _trade_to_order(trade)
        assert order.oca_group is None


class TestTradeToSorFrame:
    def test_returns_none_when_no_perm_or_order_id(self) -> None:
        trade = _make_trade(perm_id=0, order_id=0)
        assert _trade_to_sor_frame(trade) is None

    def test_returns_none_when_order_missing(self) -> None:
        trade = SimpleNamespace(order=None, orderStatus=None, contract=None, fills=[], log=[])
        assert _trade_to_sor_frame(trade) is None

    def test_maps_log_to_last_event_at(self) -> None:
        log_entry = SimpleNamespace(
            time=datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc),
            message="OK",
        )
        trade = _make_trade(perm_id=1, log=[log_entry])
        frame = _trade_to_sor_frame(trade)
        assert frame is not None
        assert frame.last_event_at is not None
        assert "2026-05-10" in frame.last_event_at

    def test_extracts_reject_reason_from_log(self) -> None:
        log_entry = SimpleNamespace(time=None, message="Order Rejected: Risk Limit")
        trade = _make_trade(perm_id=1, status="Rejected", log=[log_entry])
        frame = _trade_to_sor_frame(trade)
        assert frame is not None
        assert frame.status == "rejected"
        assert frame.reject_reason is not None and "Rejected" in frame.reject_reason

    def test_unknown_status_falls_back_to_pending(self) -> None:
        trade = _make_trade(perm_id=1, status="ExoticState")
        frame = _trade_to_sor_frame(trade)
        assert frame is not None
        assert frame.status == "pending"


# --------------------------------------------------------------------------
# Pure helper utilities
# --------------------------------------------------------------------------


class TestPureHelpers:
    def test_decimal_str_handles_int(self) -> None:
        assert _decimal_str(10) == "10"

    def test_decimal_str_handles_float(self) -> None:
        assert _decimal_str(3.14) == "3.14"

    def test_decimal_str_returns_none_for_nan(self) -> None:
        assert _decimal_str(float("nan")) is None

    def test_decimal_str_returns_none_for_invalid(self) -> None:
        assert _decimal_str("abc") is None

    def test_decimal_str_returns_none_for_none(self) -> None:
        assert _decimal_str(None) is None

    def test_to_decimal_int(self) -> None:
        assert _to_decimal(5) == 5

    def test_to_decimal_invalid(self) -> None:
        assert _to_decimal("not-a-number") is None

    def test_to_decimal_nan(self) -> None:
        assert _to_decimal(float("nan")) is None

    def test_is_finite_number_int(self) -> None:
        assert _is_finite_number(5)

    def test_is_finite_number_bool_false(self) -> None:
        assert not _is_finite_number(True)

    def test_is_finite_number_nan_false(self) -> None:
        assert not _is_finite_number(float("nan"))

    def test_is_finite_number_inf_false(self) -> None:
        assert not _is_finite_number(float("inf"))

    def test_is_finite_number_string_valid(self) -> None:
        assert _is_finite_number("3.14")

    def test_is_finite_number_string_invalid(self) -> None:
        assert not _is_finite_number("abc")


# --------------------------------------------------------------------------
# _build_ib_order (echte ib_async-Order)
# --------------------------------------------------------------------------


class TestBuildIbOrder:
    def test_lmt_order_sets_lmt_price(self) -> None:
        request = _order_request(order_type=OrderType.LMT, limit_price="100.5")
        order = _build_ib_order(request)
        assert order.action == "BUY"
        assert order.orderType == "LMT"
        assert order.lmtPrice == 100.5
        assert order.tif == "DAY"
        assert order.account == "U25235077"

    def test_mkt_order_omits_prices(self) -> None:
        request = _order_request(
            order_type=OrderType.MKT, limit_price=None, stop_price=None
        )
        order = _build_ib_order(request)
        assert order.orderType == "MKT"
        # ib_async signalisiert "kein Preis gesetzt" via UNSET_DOUBLE
        # (~1.79e308). Hauptsache, _build_ib_order hat keine eigenen
        # Werte fuer lmtPrice/auxPrice gesetzt.
        from ib_async.objects import UNSET_DOUBLE  # type: ignore[import-untyped]

        assert order.lmtPrice == UNSET_DOUBLE
        assert order.auxPrice == UNSET_DOUBLE

    def test_stp_lmt_sets_both_prices(self) -> None:
        request = _order_request(
            order_type=OrderType.STP_LMT,
            limit_price="200.5", stop_price="195.0",
        )
        order = _build_ib_order(request)
        assert order.orderType == "STP LMT"
        assert order.lmtPrice == 200.5
        assert order.auxPrice == 195.0


# --------------------------------------------------------------------------
# whatif_order (What-If-/Margin-Vorschau)
# --------------------------------------------------------------------------


def _make_order_state(
    *,
    commission: float = 1.0,
    max_commission: float = 1.5,
    commission_currency: str = "USD",
    equity_before: str = "179.54",
    equity_after: str = "91.00",
    init_margin_after: str = "88.00",
    maint_margin_after: str = "80.00",
    warning_text: str = "",
) -> SimpleNamespace:
    """Minimaler ib_async-``OrderState``-Stub fuer whatif-Tests."""
    return SimpleNamespace(
        status="PreSubmitted",
        commission=commission,
        minCommission=0.0,
        maxCommission=max_commission,
        commissionCurrency=commission_currency,
        equityWithLoanBefore=equity_before,
        equityWithLoanAfter=equity_after,
        initMarginBefore="0.00",
        initMarginAfter=init_margin_after,
        maintMarginBefore="0.00",
        maintMarginAfter=maint_margin_after,
        warningText=warning_text,
    )


def _make_account_value(
    tag: str, value: str, currency: str, account: str = "U25235077"
) -> SimpleNamespace:
    return SimpleNamespace(
        tag=tag, value=value, currency=currency, account=account, modelCode=""
    )


def _default_account_values() -> list[SimpleNamespace]:
    return [_make_account_value("NetLiquidation", "200.00", "EUR")]


def _make_whatif_client(
    *,
    state: SimpleNamespace | None = None,
    managed_accounts: list[str] | None = None,
    qualify_results: list[Any] | None = None,
    whatif_side_effect: Any = None,
    account_values: list[SimpleNamespace] | None = None,
    read_only: bool = True,
) -> MagicMock:
    qualified_default = [_make_contract()]
    ib = SimpleNamespace(
        managedAccounts=MagicMock(
            return_value=list(managed_accounts) if managed_accounts is not None else ["U25235077"]
        ),
        qualifyContractsAsync=AsyncMock(
            return_value=qualify_results if qualify_results is not None else qualified_default
        ),
        whatIfOrderAsync=AsyncMock(
            return_value=state if state is not None else _make_order_state(),
            side_effect=whatif_side_effect,
        ),
        accountValues=MagicMock(
            return_value=account_values if account_values is not None else _default_account_values()
        ),
    )
    client = MagicMock()
    client._ib = ib
    client.read_only = read_only
    return client


class TestWhatIfOrder:
    async def test_503_in_read_only(self) -> None:
        # IBKR lehnt whatIfOrder im Read-Only-API-Modus ab (Warning 321)
        # ohne OrderState -> der Call wuerde haengen. Proaktiver 503,
        # whatIfOrderAsync wird gar nicht erst gerufen.
        client = _make_whatif_client(read_only=True)
        service = TWSOrdersService(client, read_only=True)
        with pytest.raises(HTTPException) as exc_info:
            await service.whatif_order(_order_request())
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["code"] == "whatif_requires_write_session"
        client._ib.whatIfOrderAsync.assert_not_called()

    async def test_pulls_read_only_from_client_default(self) -> None:
        client = _make_whatif_client(read_only=True)
        service = TWSOrdersService(client)  # read_only=None -> vom Client
        with pytest.raises(HTTPException) as exc_info:
            await service.whatif_order(_order_request())
        assert exc_info.value.status_code == 503

    async def test_returns_preview_with_commission_and_margin(self) -> None:
        client = _make_whatif_client()
        service = TWSOrdersService(client, read_only=False)
        preview = await service.whatif_order(_order_request())
        assert isinstance(preview, WhatIfPreview)
        assert preview.account_id == "U25235077"
        assert preview.conid == 265598
        # estimated_amount = quantity(10) * limit_price(200.50) = 2005.00
        assert preview.estimated_amount is not None
        assert Decimal(preview.estimated_amount.value) == Decimal("2005.00")
        assert preview.estimated_amount.currency == "USD"
        # estimated_commission = maxCommission (1.5), commissionCurrency USD
        assert preview.estimated_commission is not None
        assert Decimal(preview.estimated_commission.value) == Decimal("1.5")
        # estimated_total = amount + commission (gleiche Currency)
        assert preview.estimated_total is not None
        assert Decimal(preview.estimated_total.value) == Decimal("2006.5")
        # margin_impact in Base-Currency (EUR aus accountValues)
        assert preview.margin_impact.current_funds is not None
        assert preview.margin_impact.current_funds.currency == "EUR"
        assert Decimal(preview.margin_impact.current_funds.value) == Decimal("179.54")
        assert preview.margin_impact.after_funds is not None
        assert Decimal(preview.margin_impact.after_funds.value) == Decimal("91.00")
        assert preview.margin_impact.init_margin_after is not None
        assert preview.margin_impact.maint_margin_after is not None

    async def test_mkt_order_has_no_estimated_amount(self) -> None:
        client = _make_whatif_client()
        service = TWSOrdersService(client, read_only=False)
        preview = await service.whatif_order(
            _order_request(order_type=OrderType.MKT, limit_price=None, stop_price=None)
        )
        # Ohne Limit-Preis keine Betragsschaetzung, Commission/Margin bleiben.
        assert preview.estimated_amount is None
        assert preview.estimated_total is None
        assert preview.estimated_commission is not None
        assert preview.margin_impact.current_funds is not None

    async def test_parses_warning_with_code(self) -> None:
        state = _make_order_state(
            warning_text=(
                "21/You are trying to submit an order without having market data "
                "for this instrument."
            )
        )
        client = _make_whatif_client(state=state)
        service = TWSOrdersService(client, read_only=False)
        preview = await service.whatif_order(_order_request())
        assert len(preview.warnings) == 1
        warn = preview.warnings[0]
        assert warn.raw_id == 21
        assert warn.code == "no_market_data_subscription"
        assert "market data" in warn.message

    async def test_no_warnings_when_warning_text_empty(self) -> None:
        client = _make_whatif_client(state=_make_order_state(warning_text=""))
        service = TWSOrdersService(client, read_only=False)
        preview = await service.whatif_order(_order_request())
        assert preview.warnings == []

    async def test_validates_account(self) -> None:
        client = _make_whatif_client(managed_accounts=["DUP799747"])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.whatif_order(_order_request())
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "invalid_account"

    async def test_invalid_conid_when_qualify_empty(self) -> None:
        client = _make_whatif_client(qualify_results=[])
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.whatif_order(_order_request())
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "invalid_conid"

    async def test_propagates_backend_error_as_502(self) -> None:
        client = _make_whatif_client(whatif_side_effect=RuntimeError("ibkr down"))
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.whatif_order(_order_request())
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["code"] == "tws_whatif_failed"

    async def test_timeout_returns_504(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Haengender whatIfOrderAsync -> asyncio.wait_for greift -> 504,
        # statt den Request endlos zu binden (Read-Only-Hang-Schutz).
        monkeypatch.setattr("broker_gateway.tws.orders._WHATIF_TIMEOUT_S", 0.01)

        async def _hang(*_a: Any, **_k: Any) -> Any:
            await asyncio.sleep(1.0)

        client = _make_whatif_client()
        client._ib.whatIfOrderAsync = _hang
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.whatif_order(_order_request())
        assert exc_info.value.status_code == 504
        assert exc_info.value.detail["code"] == "tws_whatif_timeout"

    async def test_empty_state_returns_502(self) -> None:
        client = _make_whatif_client()
        client._ib.whatIfOrderAsync = AsyncMock(return_value=None)
        service = TWSOrdersService(client, read_only=False)
        with pytest.raises(HTTPException) as exc_info:
            await service.whatif_order(_order_request())
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["code"] == "tws_whatif_empty"

    async def test_margin_none_when_base_currency_unknown(self) -> None:
        # accountValues ohne verwertbare Currency -> Margin-Money None,
        # aber Commission (eigene Currency) bleibt befuellt.
        client = _make_whatif_client(account_values=[])
        service = TWSOrdersService(client, read_only=False)
        preview = await service.whatif_order(_order_request())
        assert preview.margin_impact.current_funds is None
        assert preview.margin_impact.after_funds is None
        assert preview.estimated_commission is not None


class TestWhatIfHelpers:
    def test_parse_whatif_warning_with_code(self) -> None:
        warn = _parse_whatif_warning("21/blind trading warning")
        assert warn.raw_id == 21
        assert warn.code == "no_market_data_subscription"
        assert warn.message == "blind trading warning"

    def test_parse_whatif_warning_unknown_code(self) -> None:
        warn = _parse_whatif_warning("999/some new warning")
        assert warn.raw_id == 999
        assert warn.code == "whatif_warning"

    def test_parse_whatif_warning_without_code(self) -> None:
        warn = _parse_whatif_warning("Just a freetext warning")
        assert warn.raw_id is None
        assert warn.code == "whatif_warning"
        assert warn.message == "Just a freetext warning"

    def test_account_base_currency_from_values(self) -> None:
        ib = SimpleNamespace(
            accountValues=MagicMock(
                return_value=[
                    _make_account_value("AccountType", "INDIVIDUAL", ""),
                    _make_account_value("CashBalance", "100", "BASE"),
                    _make_account_value("NetLiquidation", "200", "EUR"),
                ]
            )
        )
        assert _account_base_currency(ib, "U25235077") == "EUR"

    def test_account_base_currency_none_when_no_currency(self) -> None:
        ib = SimpleNamespace(
            accountValues=MagicMock(
                return_value=[_make_account_value("AccountType", "INDIVIDUAL", "")]
            )
        )
        assert _account_base_currency(ib, "U25235077") is None

    def test_account_base_currency_typeerror_fallback(self) -> None:
        def _account_values(*args: Any) -> list[SimpleNamespace]:
            if args:
                raise TypeError("kein positional arg")
            return [_make_account_value("NetLiquidation", "200", "USD")]

        ib = SimpleNamespace(accountValues=_account_values)
        assert _account_base_currency(ib, "U25235077") == "USD"

    def test_account_base_currency_swallows_generic_error(self) -> None:
        ib = SimpleNamespace(
            accountValues=MagicMock(side_effect=RuntimeError("not subscribed"))
        )
        assert _account_base_currency(ib, "U25235077") is None

    def test_first_finite_returns_first_valid(self) -> None:
        assert _first_finite(None, float("nan"), 1.5, 2.0) == 1.5

    def test_first_finite_returns_none_when_all_invalid(self) -> None:
        assert _first_finite(None, float("nan"), float("inf")) is None

    def test_order_state_total_none_on_currency_mismatch(self) -> None:
        # estimated_amount USD, commission EUR -> total nicht summierbar.
        state = _make_order_state(commission_currency="EUR", max_commission=1.0)
        request = _order_request(limit_price="100")
        contract = _make_contract(currency="USD")
        preview = _order_state_to_whatif(state, request, contract, "EUR")
        assert preview.estimated_amount is not None
        assert preview.estimated_amount.currency == "USD"
        assert preview.estimated_commission is not None
        assert preview.estimated_commission.currency == "EUR"
        assert preview.estimated_total is None

    def test_order_state_commission_falls_back_to_commission_field(self) -> None:
        # maxCommission nan -> commission-Feld wird genutzt.
        state = _make_order_state(commission=2.0, max_commission=float("nan"))
        request = _order_request()
        contract = _make_contract(currency="USD")
        preview = _order_state_to_whatif(state, request, contract, "USD")
        assert preview.estimated_commission is not None
        assert Decimal(preview.estimated_commission.value) == Decimal("2.0")
