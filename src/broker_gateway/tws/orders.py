"""Order-Adapter gegen die TWS-Socket-API (ib_async).

Pendant zu :class:`broker_gateway.cp.orders.OrdersService` plus
:class:`broker_gateway.api.v1.orders_stream.OrdersBootstrapLoader`. Der
TWS-Pfad bedient ``POST/GET/DELETE /v1/orders``, ``/v1/orders/stream``
(SSE) und ``/v1/orders/ws`` (WebSocket) aus einem zusammenhaengenden
Service-Objekt.

Schema-Identitaet: liefert dieselben Pydantic-Modelle (``Order``,
``OrderCancellation``) wie ``cp.OrdersService`` und dieselben
``SorFrame``-Strukturen wie ``cp.topics.sor`` - damit Konsumenten den
Backend-Switch nicht sehen.

ib_async-Patterns (siehe Memory ``project_tws_api_pi5_setup``):

- ``IB.placeOrder(contract, order)`` → ``Trade`` (Order + Status + Fills).
- ``IB.openTrades()`` synchron, fuer Bootstrap.
- ``IB.cancelOrder(order)`` triggert ``orderStatusEvent`` mit Cancelled.
- ``IB.openOrderEvent`` / ``IB.orderStatusEvent`` / ``IB.execDetailsEvent``
  feuern fuer den Stream-Pfad - der Pump unten haengt sich dort ein und
  publish-t ueber den ``OrdersBroadcaster``.

Order-ID-Strategie: ``permId`` ist persistent ueber Sitzungen hinweg und
wird als primaere ``order_id`` zurueckgegeben. Solange IBKR ``permId``
noch nicht gesetzt hat (typisch direkt nach ``placeOrder``), faellt der
Adapter auf ``str(orderId)`` zurueck. Lookup-Pfade akzeptieren beide
Varianten.

AP ``2a203c58-...`` Phase 4.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, status

from broker_gateway.cp.topics.sor import SorFrame
from broker_gateway.money import Money, normalize_money
from broker_gateway.order_models import (
    MarginImpact,
    Order,
    OrderCancellation,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    WhatIfPreview,
    WhatIfWarning,
)
from broker_gateway.streams.orders import OrdersBroadcaster

if TYPE_CHECKING:
    from broker_gateway.tws.client import TWSClient


logger = logging.getLogger(__name__)


__all__ = [
    "TWSOrdersService",
    "TWSOrdersBootstrapLoader",
    "TWSOrdersStreamPump",
]


_FALLBACK_CURRENCY = "USD"

# Hartes Timeout um ``whatIfOrderAsync``. IBKR liefert bei manchen
# Fehlersituationen (z.B. fehlende Marktdaten in einer Write-Session)
# keinen OrderState und auch kein Order-Ende - ib_async wartet dann
# endlos. Defense-in-Depth zusaetzlich zum read_only-Gate.
_WHATIF_TIMEOUT_S = 15.0


# IBKR-Status (ib_async ``Order.status``/``Trade.orderStatus.status``) ->
# cp-OrderStatus (Pydantic-Enum). Keys werden case-insensitiv gematcht.
_IB_TO_CP_STATUS: dict[str, OrderStatus] = {
    "PENDINGSUBMIT": OrderStatus.PENDING_SUBMIT,
    "PRESUBMITTED": OrderStatus.SUBMITTED,
    "SUBMITTED": OrderStatus.SUBMITTED,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "CANCELED": OrderStatus.CANCELLED,
    "PENDINGCANCEL": OrderStatus.SUBMITTED,
    "APIPENDING": OrderStatus.PENDING_SUBMIT,
    "REJECTED": OrderStatus.REJECTED,
    "INACTIVE": OrderStatus.INACTIVE,
    "APICANCELLED": OrderStatus.CANCELLED,
}

# IBKR-Order-Status -> sor-Topic-Status (semantisch reduziert). Match
# zur SorTopicAdapter-Tabelle, damit das SSE/WS-Schema bitidentisch zum
# cp-Pfad bleibt. Default ``pending`` fuer unbekannte Werte.
_IB_TO_SOR_STATUS: dict[str, str] = {
    "PENDINGSUBMIT": "pending",
    "PRESUBMITTED": "accepted",
    "SUBMITTED": "accepted",
    "PARTIALFILL": "partial_fill",
    "PARTIALLYFILLED": "partial_fill",
    "FILLED": "filled",
    "PENDINGCANCEL": "pending",
    "CANCELLED": "cancelled",
    "CANCELED": "cancelled",
    "APIPENDING": "pending",
    "APICANCELLED": "cancelled",
    "REJECTED": "rejected",
    "INACTIVE": "pending",
}


class TWSOrdersService:
    """ib_async-basierter Orders-Service.

    Kapselt place / get / cancel / list-open. Der Stream-Pfad sitzt in
    :class:`TWSOrdersStreamPump` und publisht ueber den gemeinsamen
    :class:`broker_gateway.streams.orders.OrdersBroadcaster`.
    """

    def __init__(
        self,
        client: "TWSClient",
        *,
        read_only: bool | None = None,
    ) -> None:
        self._client = client
        # ``read_only`` darf im Konstruktor explizit gesetzt werden
        # (Tests + spaetere Override-Pfade). Default zieht den Wert vom
        # TWSClient.
        if read_only is None:
            read_only = bool(getattr(client, "read_only", True))
        self._read_only = bool(read_only)

    @property
    def read_only(self) -> bool:
        return self._read_only

    # ---- Schreib-Pfade -----------------------------------------------

    async def place_order(self, request: OrderRequest) -> Order:
        if self._read_only:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "read_only_api",
                    "message": "Order-Routing ist im read-only Modus deaktiviert",
                },
            )

        ib = self._client._ib  # noqa: SLF001
        managed = list(ib.managedAccounts() or [])
        if managed and request.account_id not in managed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_account",
                    "message": (
                        f"account_id={request.account_id} ist nicht in den "
                        f"verfuegbaren Accounts ({', '.join(managed)})"
                    ),
                },
            )

        contract_class = await self._import_contract_class()
        qualified = await self._qualify(contract_class, int(request.conid))
        if qualified is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_conid",
                    "message": f"conid={request.conid} konnte nicht qualifiziert werden",
                },
            )

        order_obj = _build_ib_order(request)
        try:
            trade = ib.placeOrder(qualified, order_obj)
        except Exception as exc:  # noqa: BLE001
            logger.warning("placeOrder fehlgeschlagen: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "tws_place_failed",
                    "message": f"placeOrder via TWS fehlgeschlagen: {exc}",
                },
            ) from exc
        return _trade_to_order(trade, request=request)

    async def cancel_order(self, account_id: str, order_id: str) -> OrderCancellation:
        ib = self._client._ib  # noqa: SLF001
        trade = _find_trade(ib, order_id, account_id=account_id)
        if trade is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="order_id unbekannt",
            )
        try:
            ib.cancelOrder(trade.order)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancelOrder fehlgeschlagen: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "tws_cancel_failed",
                    "message": f"cancelOrder via TWS fehlgeschlagen: {exc}",
                },
            ) from exc
        return OrderCancellation(
            order_id=str(order_id),
            cancelled_at=datetime.now(timezone.utc),
        )

    # ---- Lese-Pfade --------------------------------------------------

    async def get_order(self, order_id: str) -> Order:
        ib = self._client._ib  # noqa: SLF001
        trade = _find_trade(ib, order_id)
        if trade is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="order_id unbekannt",
            )
        return _trade_to_order(trade)

    async def list_open(self) -> list[Order]:
        ib = self._client._ib  # noqa: SLF001
        return [_trade_to_order(trade) for trade in _open_trades(ib)]

    # ---- What-If-Vorschau --------------------------------------------

    async def whatif_order(self, request: OrderRequest) -> WhatIfPreview:
        """Margin-/Risk-Vorschau ohne Order-Platzierung.

        **Erfordert eine nicht-read-only TWS-Session.** IBKR behandelt
        ``whatIfOrder`` als Order-Validierung und lehnt ihn im
        Read-Only-API-Modus ab (``Warning 321: The API interface is
        currently in Read-Only mode``) - liefert dabei aber **keinen**
        OrderState, sodass ``whatIfOrderAsync`` endlos haengen wuerde.
        Daher: im read_only-Modus sofort 503 (wie ``place_order``).
        whatif gehoert funktional zum Schreib-Workflow - wer nicht
        platzieren darf, hat fuer eine Vorschau keinen Nutzen. Der
        Token-*Scope* (``orders:read``) regelt nur die Berechtigung; die
        Session-*Faehigkeit* (write) ist davon unabhaengig. Verifiziert
        2026-06-08 im Paper-Smoke (DUP799747, read_only=yes).

        ``whatIfOrderAsync`` ist die async-Variante (der synchrone
        Wrapper wuerde den FastAPI-Event-Loop blockieren), zusaetzlich
        mit ``asyncio.wait_for`` gegen Haenger abgesichert.
        """
        if self._read_only:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "whatif_requires_write_session",
                    "message": (
                        "What-If-Vorschau erfordert eine nicht-read-only "
                        "TWS-Session; IBKR lehnt whatIfOrder im "
                        "Read-Only-API-Modus ab (Warning 321)"
                    ),
                },
            )

        ib = self._client._ib  # noqa: SLF001
        managed = list(ib.managedAccounts() or [])
        if managed and request.account_id not in managed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_account",
                    "message": (
                        f"account_id={request.account_id} ist nicht in den "
                        f"verfuegbaren Accounts ({', '.join(managed)})"
                    ),
                },
            )

        contract_class = await self._import_contract_class()
        qualified = await self._qualify(contract_class, int(request.conid))
        if qualified is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_conid",
                    "message": f"conid={request.conid} konnte nicht qualifiziert werden",
                },
            )

        order_obj = _build_ib_order(request)
        try:
            state = await asyncio.wait_for(
                ib.whatIfOrderAsync(qualified, order_obj),
                timeout=_WHATIF_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            logger.warning("whatIfOrderAsync Timeout nach %ss", _WHATIF_TIMEOUT_S)
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail={
                    "code": "tws_whatif_timeout",
                    "message": (
                        f"whatIfOrder lieferte kein OrderState binnen "
                        f"{_WHATIF_TIMEOUT_S:g}s"
                    ),
                },
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatIfOrderAsync fehlgeschlagen: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "tws_whatif_failed",
                    "message": f"whatIfOrder via TWS fehlgeschlagen: {exc}",
                },
            ) from exc
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "tws_whatif_empty",
                    "message": "whatIfOrder lieferte kein OrderState",
                },
            )

        base_currency = _account_base_currency(ib, request.account_id)
        return _order_state_to_whatif(state, request, qualified, base_currency)

    # ---- Helpers -----------------------------------------------------

    async def _qualify(self, contract_class: Any, cid: int) -> Any | None:
        ib = self._client._ib  # noqa: SLF001
        contract = contract_class(conId=cid, exchange="SMART")
        try:
            qualified = await ib.qualifyContractsAsync(contract)
        except Exception as exc:  # noqa: BLE001
            logger.warning("qualifyContractsAsync conid=%s gescheitert: %s", cid, exc)
            return None
        if not qualified:
            return None
        first = qualified[0]
        if isinstance(first, list):
            if not first or first[0] is None:
                return None
            return first[0]
        return first

    async def _import_contract_class(self) -> Any:
        try:
            from ib_async.contract import Contract  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ib_async ist nicht installiert",
            )
        return Contract


class TWSOrdersBootstrapLoader:
    """Liefert beim Subscribe-Start einen Voll-Snapshot offener Orders.

    Nutzt ``IB.openTrades()`` als Quelle (synchroner Cache, der durch
    ``reqAllOpenOrders`` und Lifecycle-Events vom IB-Gateway gefuettert
    wird). API-Vertrag identisch zur cp-Variante
    :class:`broker_gateway.api.v1.orders_stream.OrdersBootstrapLoader`.
    """

    def __init__(self, client: "TWSClient") -> None:
        self._client = client

    async def load(self, account: str) -> list[SorFrame]:
        ib = self._client._ib  # noqa: SLF001
        frames: list[SorFrame] = []
        for trade in _open_trades(ib):
            order = getattr(trade, "order", None)
            if order is None:
                continue
            order_account = getattr(order, "account", "") or ""
            if account and order_account and order_account != account:
                continue
            frame = _trade_to_sor_frame(trade)
            if frame is not None:
                frames.append(frame)
        return frames


class TWSOrdersStreamPump:
    """Brueckt ib_async-Order-Events in den ``OrdersBroadcaster``.

    Subscribed beim ``start()`` an ``openOrderEvent``,
    ``orderStatusEvent`` und ``execDetailsEvent`` und publish-t pro
    Event einen :class:`SorFrame` ueber den Broadcaster. Der Pump merkt
    sich, ob er angeklemmt ist - wiederholtes ``start()`` ist no-op.
    """

    def __init__(
        self,
        client: "TWSClient",
        broadcaster: OrdersBroadcaster,
    ) -> None:
        self._client = client
        self._broadcaster = broadcaster
        self._attached = False

    @property
    def attached(self) -> bool:
        return self._attached

    def start(self) -> None:
        if self._attached:
            return
        ib = self._client._ib  # noqa: SLF001
        for event_name in ("openOrderEvent", "orderStatusEvent"):
            event = getattr(ib, event_name, None)
            if event is not None:
                event += self._on_trade_event
        exec_event = getattr(ib, "execDetailsEvent", None)
        if exec_event is not None:
            exec_event += self._on_exec_event
        self._attached = True

    def stop(self) -> None:
        if not self._attached:
            return
        ib = self._client._ib  # noqa: SLF001
        for event_name in ("openOrderEvent", "orderStatusEvent"):
            event = getattr(ib, event_name, None)
            if event is not None:
                try:
                    event -= self._on_trade_event
                except Exception:  # noqa: BLE001 - best effort
                    pass
        exec_event = getattr(ib, "execDetailsEvent", None)
        if exec_event is not None:
            try:
                exec_event -= self._on_exec_event
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._attached = False

    def _on_trade_event(self, trade: Any) -> None:
        self._publish(trade)

    def _on_exec_event(self, trade: Any, _fill: Any) -> None:
        self._publish(trade)

    def _publish(self, trade: Any) -> None:
        frame = _trade_to_sor_frame(trade)
        if frame is None:
            return
        account = frame.account or ""
        if not account:
            return
        try:
            self._broadcaster.publish(account, frame)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OrdersBroadcaster.publish fehlgeschlagen: %s", exc)


# ---- Mapping-Helper --------------------------------------------------


def _build_ib_order(request: OrderRequest) -> Any:
    """Baut ein ib_async-Order-Objekt aus dem cp-OrderRequest.

    Nutzt einen lazy Import, damit die Tests ohne ib_async-Installation
    durchlaufen koennen (Stub-Pfad).
    """
    try:
        from ib_async.order import Order as IBOrder  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ib_async ist nicht installiert",
        )
    order_type_map = {
        OrderType.LMT: "LMT",
        OrderType.MKT: "MKT",
        OrderType.STP: "STP",
        OrderType.STP_LMT: "STP LMT",
    }
    kwargs: dict[str, Any] = {
        "action": request.side.value,
        "totalQuantity": float(Decimal(request.quantity)),
        "orderType": order_type_map[request.order_type],
        "tif": request.tif.value,
        "account": request.account_id,
    }
    if request.limit_price is not None:
        kwargs["lmtPrice"] = float(Decimal(request.limit_price))
    if request.stop_price is not None:
        kwargs["auxPrice"] = float(Decimal(request.stop_price))
    return IBOrder(**kwargs)


def _trade_to_order(trade: Any, *, request: OrderRequest | None = None) -> Order:
    """Wandelt ein ib_async-Trade in unser ``Order``-Schema."""
    order = getattr(trade, "order", None)
    order_status_obj = getattr(trade, "orderStatus", None)
    contract = getattr(trade, "contract", None)

    raw_status = (
        getattr(order_status_obj, "status", None)
        or getattr(order, "status", None)
        or "Submitted"
    )
    cp_status = _IB_TO_CP_STATUS.get(
        str(raw_status).upper().replace(" ", ""), OrderStatus.SUBMITTED
    )

    side_raw = (getattr(order, "action", None) or (request.side.value if request else "BUY")).upper()
    try:
        side = OrderSide(side_raw)
    except ValueError:
        side = request.side if request else OrderSide.BUY

    type_raw = (getattr(order, "orderType", None) or (request.order_type.value if request else "MKT")).upper()
    type_norm = type_raw.replace(" ", "-")
    try:
        order_type = OrderType(type_norm)
    except ValueError:
        order_type = request.order_type if request else OrderType.MKT

    tif_raw = (getattr(order, "tif", None) or (request.tif.value if request else "DAY")).upper()
    try:
        tif = TimeInForce(tif_raw)
    except ValueError:
        tif = request.tif if request else TimeInForce.DAY

    quantity_raw = getattr(order, "totalQuantity", None)
    if quantity_raw is None and request is not None:
        quantity_raw = request.quantity
    quantity_str = _decimal_str(quantity_raw) or "0"

    filled_raw = getattr(order_status_obj, "filled", None)
    filled_str = _decimal_str(filled_raw) if filled_raw is not None else None

    avg_price_raw = getattr(order_status_obj, "avgFillPrice", None)
    currency = getattr(contract, "currency", None) or _FALLBACK_CURRENCY
    avg_fill_price = (
        normalize_money(avg_price_raw, currency)
        if avg_price_raw is not None and _is_finite_number(avg_price_raw) and float(avg_price_raw) > 0
        else None
    )

    commission = _commission_from_trade(trade, currency)

    perm_id = getattr(order, "permId", 0) or 0
    order_id = getattr(order, "orderId", 0) or 0
    primary_id = perm_id if perm_id else order_id
    order_id_str = str(primary_id) if primary_id else "0"

    account_id = (
        getattr(order, "account", None)
        or (request.account_id if request else "")
    )

    conid = (
        getattr(contract, "conId", None)
        if contract is not None
        else (request.conid if request else 0)
    ) or (request.conid if request else 0)

    limit_price = _decimal_str(getattr(order, "lmtPrice", None))
    stop_price = _decimal_str(getattr(order, "auxPrice", None))
    if limit_price is None and request is not None:
        limit_price = request.limit_price
    if stop_price is None and request is not None:
        stop_price = request.stop_price

    # OCA-Gruppe (Karte def3e8f5): ib_async setzt ocaGroup per Default auf
    # den leeren String - zu None normalisieren, damit "keine OCA-Gruppe"
    # eindeutig ist.
    oca_group = _str_or_none(getattr(order, "ocaGroup", None))

    return Order(
        order_id=order_id_str,
        account_id=str(account_id),
        conid=int(conid or 0),
        side=side,
        quantity=quantity_str,
        order_type=order_type,
        tif=tif,
        status=cp_status,
        limit_price=limit_price,
        stop_price=stop_price,
        oca_group=oca_group,
        avg_fill_price=avg_fill_price,
        commission=commission,
        filled_quantity=filled_str,
        warnings=[],
    )


def _trade_to_sor_frame(trade: Any) -> SorFrame | None:
    order = getattr(trade, "order", None)
    contract = getattr(trade, "contract", None)
    order_status_obj = getattr(trade, "orderStatus", None)
    if order is None:
        return None

    perm_id = getattr(order, "permId", 0) or 0
    raw_order_id = getattr(order, "orderId", 0) or 0
    primary_id = perm_id if perm_id else raw_order_id
    if not primary_id:
        return None

    raw_status = (
        getattr(order_status_obj, "status", None)
        or getattr(order, "status", None)
        or "Submitted"
    )
    sor_status = _IB_TO_SOR_STATUS.get(
        str(raw_status).upper().replace(" ", ""), "pending"
    )

    last_event_at: str | None = None
    log = getattr(trade, "log", None)
    if log:
        try:
            last_entry = log[-1]
            ts = getattr(last_entry, "time", None)
            if isinstance(ts, datetime):
                last_event_at = ts.isoformat()
        except (IndexError, TypeError):
            last_event_at = None

    reject_reason = None
    if log:
        try:
            for entry in reversed(list(log)):
                msg = getattr(entry, "message", None) or getattr(entry, "errorCode", None)
                if msg and "reject" in str(msg).lower():
                    reject_reason = str(msg)
                    break
        except Exception:  # noqa: BLE001
            reject_reason = None

    quantity = _to_decimal(getattr(order, "totalQuantity", None))
    filled = _to_decimal(getattr(order_status_obj, "filled", None))
    avg_price = _to_decimal(getattr(order_status_obj, "avgFillPrice", None))

    return SorFrame(
        order_id=int(primary_id),
        account=getattr(order, "account", None) or None,
        client_order_id=_str_or_none(getattr(order, "orderRef", None)),
        parent_id=int(getattr(order, "parentId", 0) or 0) or None,
        symbol=_str_or_none(getattr(contract, "symbol", None)),
        side=_str_or_none(getattr(order, "action", None)),
        quantity=quantity,
        filled_quantity=filled,
        avg_fill_price=avg_price,
        status=sor_status,
        time_in_force=_str_or_none(getattr(order, "tif", None)),
        last_event_at=last_event_at,
        reject_reason=reject_reason,
        conid=int(getattr(contract, "conId", 0) or 0) or None,
    )


def _open_trades(ib: Any) -> list[Any]:
    fn = getattr(ib, "openTrades", None)
    if callable(fn):
        result = fn() or []
        return list(result)
    fn = getattr(ib, "trades", None)
    if callable(fn):
        result = fn() or []
        return list(result)
    return []


def _all_trades(ib: Any) -> list[Any]:
    fn = getattr(ib, "trades", None)
    if callable(fn):
        result = fn() or []
        return list(result)
    fn = getattr(ib, "openTrades", None)
    if callable(fn):
        result = fn() or []
        return list(result)
    return []


def _find_trade(
    ib: Any,
    order_id: str,
    *,
    account_id: str | None = None,
) -> Any | None:
    """Sucht in allen bekannten Trades nach permId oder orderId.

    Bevorzugt ``permId`` (persistent), faellt auf ``orderId`` zurueck.
    Wenn ``account_id`` gesetzt ist, muss zusaetzlich der Account
    matchen - falls IBKR mehrere Accounts in einer Session haelt.
    """
    try:
        target_int = int(order_id)
    except (TypeError, ValueError):
        return None
    candidates = _all_trades(ib)
    for trade in candidates:
        order = getattr(trade, "order", None)
        if order is None:
            continue
        perm_id = getattr(order, "permId", 0) or 0
        ord_id = getattr(order, "orderId", 0) or 0
        if perm_id != target_int and ord_id != target_int:
            continue
        if account_id:
            order_account = getattr(order, "account", "") or ""
            if order_account and order_account != account_id:
                continue
        return trade
    return None


def _commission_from_trade(trade: Any, currency: str) -> Any:
    """Aggregiert Fill-Commissions zu einem ``Money``-Wert.

    ib_async legt fuer jeden Fill eine ``CommissionReport`` an. Wir
    summieren die einzelnen Commissions zu einer Gesamt-Commission;
    Fehlt der Wert, geben wir ``None`` zurueck.
    """
    fills = getattr(trade, "fills", None) or []
    total = Decimal("0")
    seen = False
    for fill in fills:
        report = getattr(fill, "commissionReport", None)
        if report is None:
            continue
        value = getattr(report, "commission", None)
        if value is None or not _is_finite_number(value):
            continue
        try:
            total += Decimal(str(value))
            seen = True
        except (InvalidOperation, ValueError):
            continue
    if not seen:
        return None
    return normalize_money(total, currency)


# ---- What-If-Mapping -------------------------------------------------


# IBKR-Warning-Code (raw_id) -> semantischer code. Quelle:
# docs/research/ibkr-whatif-warnings.md. Unbekannte Codes -> "whatif_warning".
_WHATIF_WARNING_CODES: dict[int, str] = {
    1: "no_market_data_subscription",
    4: "price_check_unavailable",
    21: "no_market_data_subscription",
    23: "market_order_confirmation",
    26: "mandatory_cap_price",
}


def _account_base_currency(ib: Any, account: str) -> str | None:
    """Ermittelt die Account-Base-Currency aus dem ib_async-Cache.

    Spiegelt das Pattern aus ``tws.portfolio`` (NetLiquidation/
    EquityWithLoanValue tragen die Base-Currency). ``None``, wenn der
    Account (noch) nicht subscribed ist - dann bleiben die Margin-Money-
    Felder ``None`` statt erfunden.
    """
    try:
        values = ib.accountValues(account)
    except TypeError:
        values = ib.accountValues()
    except Exception:  # noqa: BLE001 - best effort, fehlende Subscription
        return None
    for av in values or []:
        tag = getattr(av, "tag", "")
        currency = getattr(av, "currency", "") or ""
        if not currency or currency == "BASE":
            continue
        if tag in ("NetLiquidation", "EquityWithLoanValue", "TotalCashValue"):
            return currency
    return None


def _first_finite(*values: Any) -> Any | None:
    """Erstes finites (nicht-nan/inf) numerisches Element, sonst ``None``."""
    for value in values:
        if value is None:
            continue
        if _is_finite_number(value):
            return value
    return None


def _parse_whatif_warning(text: str) -> WhatIfWarning:
    """Parst eine IBKR-Warning. Format ``"21/message..."`` -> raw_id=21.

    ib_async ``OrderState.warningText`` ist meist reiner Freitext ohne
    Code-Praefix; dann bleibt ``raw_id`` ``None`` und ``code`` generisch.
    """
    stripped = text.strip()
    match = re.match(r"^\s*(\d+)\s*/\s*(.*)$", stripped, re.DOTALL)
    if match:
        raw_id = int(match.group(1))
        message = match.group(2).strip()
        code = _WHATIF_WARNING_CODES.get(raw_id, "whatif_warning")
        return WhatIfWarning(code=code, raw_id=raw_id, message=message)
    return WhatIfWarning(code="whatif_warning", raw_id=None, message=stripped)


def _order_state_to_whatif(
    state: Any,
    request: OrderRequest,
    contract: Any,
    base_currency: str | None,
) -> WhatIfPreview:
    """Wandelt ein ib_async ``OrderState`` in unser ``WhatIfPreview``."""
    contract_currency = getattr(contract, "currency", None) or _FALLBACK_CURRENCY

    # Commission: maxCommission konservativ, sonst commission.
    commission_currency = getattr(state, "commissionCurrency", None) or contract_currency
    comm_value = _first_finite(
        getattr(state, "maxCommission", None),
        getattr(state, "commission", None),
    )
    estimated_commission = (
        normalize_money(comm_value, commission_currency) if comm_value is not None else None
    )

    # Betrag: quantity * limit_price - nur wenn ein Limit-Preis vorliegt.
    estimated_amount: Money | None = None
    if request.limit_price is not None:
        try:
            amount = Decimal(request.quantity) * Decimal(request.limit_price)
            estimated_amount = normalize_money(amount, contract_currency)
        except (InvalidOperation, ValueError):
            estimated_amount = None

    # Total: amount + commission, nur bei identischer Currency summierbar.
    estimated_total: Money | None = None
    if estimated_amount is not None and estimated_commission is not None:
        if estimated_amount.currency == estimated_commission.currency:
            total = Decimal(estimated_amount.value) + Decimal(estimated_commission.value)
            estimated_total = normalize_money(total, estimated_amount.currency)
    elif estimated_amount is not None and estimated_commission is None:
        estimated_total = estimated_amount

    margin = MarginImpact(
        current_funds=normalize_money(
            _to_decimal(getattr(state, "equityWithLoanBefore", None)), base_currency
        ),
        after_funds=normalize_money(
            _to_decimal(getattr(state, "equityWithLoanAfter", None)), base_currency
        ),
        init_margin_after=normalize_money(
            _to_decimal(getattr(state, "initMarginAfter", None)), base_currency
        ),
        maint_margin_after=normalize_money(
            _to_decimal(getattr(state, "maintMarginAfter", None)), base_currency
        ),
    )

    warnings: list[WhatIfWarning] = []
    warning_text = getattr(state, "warningText", None)
    if warning_text and str(warning_text).strip():
        warnings.append(_parse_whatif_warning(str(warning_text)))

    return WhatIfPreview(
        account_id=request.account_id,
        conid=int(request.conid),
        estimated_amount=estimated_amount,
        estimated_commission=estimated_commission,
        estimated_total=estimated_total,
        margin_impact=margin,
        warnings=warnings,
    )


def _decimal_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and not _is_finite_number(value):
            return None
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and not _is_finite_number(value):
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, Decimal)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, str):
        try:
            Decimal(value)
            return True
        except (InvalidOperation, ValueError):
            return False
    return False
