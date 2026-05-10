"""Trades-Adapter gegen die TWS-Socket-API (ib_async).

Pendant zu :class:`broker_gateway.cp.trades.TradesService`. Liefert
dieselben Pydantic-Modelle (``Trade``, ``TradesAggregate``), damit der
HTTP-API-Layer beim Backend-Switch keine Schema-Drift sieht.

ib_async-Patterns (siehe Memory ``project_tws_api_pi5_setup``):

- ``IB.executions()`` / ``IB.fills()`` synchron - Cache der aktuellen
  Session, gefuettert durch ``execDetailsEvent``.
- ``IB.reqExecutionsAsync(ExecutionFilter)`` zieht historische
  Executions (typisch ``time=YYYYMMDD-HH:MM:SS``).

Currency-Quelle: ``Contract.currency`` (z.B. USD, EUR). Falls die
CommissionReport eine eigene Currency mitbringt, gewinnt diese fuer
das ``commission``-Feld; sonst faellt der Adapter auf die Contract-
Currency zurueck. Bleibt beides leer, wird ``USD`` angenommen und der
Trade als ``currency_assumed=True`` markiert - bitidentisch zum
cp-Verhalten.

AP ``2a203c58-...`` Phase 4.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from broker_gateway.cp.trades import Trade, TradesAggregate
from broker_gateway.money import Money, normalize_money

if TYPE_CHECKING:
    from broker_gateway.tws.client import TWSClient


logger = logging.getLogger(__name__)


__all__ = ["TWSTradesService", "Trade", "TradesAggregate"]


_FALLBACK_CURRENCY = "USD"


class TWSTradesService:
    """ib_async-basierter Trades-Service."""

    def __init__(self, client: "TWSClient") -> None:
        self._client = client

    async def list_trades(
        self,
        *,
        period_from: date,
        period_to: date,
        account_id: str | None = None,
        today: date | None = None,
    ) -> list[Trade]:
        if period_to < period_from:
            from fastapi import HTTPException, status as http_status

            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="`to` muss >= `from` sein",
            )
        fills = await self._fetch_fills(period_from)
        results: list[Trade] = []
        for fill in fills:
            trade = _fill_to_trade(fill)
            if trade is None:
                continue
            if not _within_window(trade.executed_at, period_from, period_to):
                continue
            if account_id and trade.account_id and trade.account_id != account_id:
                continue
            results.append(trade)
        return results

    async def commissions_mtd(
        self,
        *,
        account_id: str | None = None,
        today: date | None = None,
    ) -> TradesAggregate:
        ref = today or datetime.now(timezone.utc).date()
        period_from = ref.replace(day=1)
        trades = await self.list_trades(
            period_from=period_from,
            period_to=ref,
            account_id=account_id,
            today=ref,
        )
        total = Decimal("0")
        currency: str | None = None
        any_assumed = False
        for trade in trades:
            if trade.commission is None:
                continue
            if currency is None:
                currency = trade.commission.currency
            if trade.commission.currency != currency:
                continue
            total += Decimal(trade.commission.value)
            if trade.currency_assumed:
                any_assumed = True
        currency = currency or _FALLBACK_CURRENCY
        return TradesAggregate(
            metric="commissions_mtd",
            period_from=period_from,
            period_to=ref,
            account_id=account_id,
            value=Money(value=str(total), currency=currency),
            trade_count=len(trades),
            currency_assumption=_FALLBACK_CURRENCY if any_assumed else None,
        )

    # ---- Helpers -----------------------------------------------------

    async def _fetch_fills(self, period_from: date) -> list[Any]:
        ib = self._client._ib  # noqa: SLF001
        # reqExecutionsAsync filtert serverseitig nach time. Fuer den
        # heutigen Tag reicht der lokale fills()-Cache; wir stellen aber
        # immer eine reqExecutions-Anfrage, damit der Tagesrand und
        # historische Ranges (>1d) gleich gut bedient werden.
        filter_obj = await self._build_execution_filter(period_from)
        results: list[Any] = []
        if filter_obj is not None and hasattr(ib, "reqExecutionsAsync"):
            try:
                results = list(await ib.reqExecutionsAsync(filter_obj))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reqExecutionsAsync fehlgeschlagen, fallback fills(): %s", exc
                )
                results = []
        if not results:
            fills_fn = getattr(ib, "fills", None)
            if callable(fills_fn):
                try:
                    results = list(fills_fn() or [])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ib.fills() fehlgeschlagen: %s", exc)
                    results = []
        return results

    async def _build_execution_filter(self, period_from: date) -> Any | None:
        try:
            from ib_async.objects import ExecutionFilter  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover
            return None
        # IBKR-Format ist YYYYMMDD-HH:MM:SS (Zeitzone-agnostisch). Wir
        # nehmen Mitternacht UTC am Start-Tag.
        time_str = datetime.combine(period_from, time(0, 0, 0)).strftime(
            "%Y%m%d-%H:%M:%S"
        )
        try:
            return ExecutionFilter(time=time_str)
        except TypeError:
            # Manche ib_async-Versionen erwarten anderen Konstruktor;
            # ohne Filter klappt fills() trotzdem.
            return None


def _fill_to_trade(fill: Any) -> Trade | None:
    execution = getattr(fill, "execution", None)
    contract = getattr(fill, "contract", None)
    report = getattr(fill, "commissionReport", None)
    if execution is None:
        return None

    contract_currency = (
        (getattr(contract, "currency", None) or "").upper() or None
        if contract is not None
        else None
    )
    report_currency = (
        (getattr(report, "currency", None) or "").upper() or None
        if report is not None
        else None
    )
    price_currency = contract_currency or _FALLBACK_CURRENCY
    commission_currency = report_currency or contract_currency or _FALLBACK_CURRENCY
    currency_assumed = contract_currency is None

    shares_raw = getattr(execution, "shares", None)
    price_raw = getattr(execution, "price", None)
    quantity_str = _decimal_str(shares_raw)

    price_money = (
        normalize_money(price_raw, price_currency)
        if _is_finite_number(price_raw)
        else None
    )

    net_amount: Money | None = None
    if _is_finite_number(price_raw) and _is_finite_number(shares_raw):
        try:
            net = Decimal(str(price_raw)) * Decimal(str(shares_raw))
            net_amount = normalize_money(net, price_currency)
        except (InvalidOperation, ValueError):
            net_amount = None

    commission_value = (
        getattr(report, "commission", None) if report is not None else None
    )
    commission_money = (
        normalize_money(commission_value, commission_currency)
        if _is_finite_number(commission_value)
        else None
    )

    executed_at = _coerce_datetime(getattr(execution, "time", None))
    perm_id = getattr(execution, "permId", 0) or 0
    raw_order_id = getattr(execution, "orderId", 0) or 0
    primary_id = perm_id if perm_id else raw_order_id

    account_id = (
        getattr(execution, "acctNumber", None)
        or getattr(execution, "accountCode", None)
        or getattr(execution, "account", None)
    )

    conid_value = getattr(contract, "conId", None) if contract is not None else None
    symbol = getattr(contract, "symbol", None) if contract is not None else None

    has_commission = commission_money is not None
    has_price = price_money is not None
    return Trade(
        execution_id=str(getattr(execution, "execId", "") or ""),
        order_id=str(primary_id) if primary_id else None,
        account_id=_str_or_none(account_id),
        conid=int(conid_value) if conid_value is not None else None,
        symbol=_str_or_none(symbol),
        side=_str_or_none(getattr(execution, "side", None)),
        quantity=quantity_str,
        price=price_money,
        net_amount=net_amount,
        commission=commission_money,
        executed_at=executed_at,
        currency_assumed=currency_assumed and (has_commission or has_price),
    )


def _within_window(executed_at: datetime | None, period_from: date, period_to: date) -> bool:
    if executed_at is None:
        return True
    d = executed_at.date()
    return period_from <= d <= period_to


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    # IBKR-Format ohne T-Trenner: YYYYMMDD-HH:MM:SS oder YYYYMMDD HH:MM:SS
    formats = (
        "%Y%m%d-%H:%M:%S",
        "%Y%m%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _decimal_str(value: Any) -> str | None:
    if value is None:
        return None
    if not _is_finite_number(value):
        return None
    try:
        return str(Decimal(str(value)))
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
