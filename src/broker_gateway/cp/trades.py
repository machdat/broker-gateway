"""Trades-Adapter gegen das CP-Gateway.

Das CP-Gateway exponiert Ausfuehrungen als
`GET /iserver/account/trades?days=N` mit `N` zwischen 1 und 30. Der
Service uebersetzt die HTTP-API-Konvention `from=YYYY-MM-DD&to=YYYY-MM-DD`
in einen passenden `days`-Wert (heute - from), holt die Liste und filtert
clientseitig nach.

Currency-Assumption: das CP-Gateway liefert `commission` haeufig ohne
explizite Currency. Der Service nimmt in dem Fall `USD` an (US-Boerse
ist Default-Trading-Account fuer den Live-Account U25235077). Die
Annahme wird in der Aggregat-Response als `currency_assumption: "USD"`
explizit kommuniziert.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.money import Money, normalize_money


_MIN_DAYS = 1
_MAX_DAYS = 30
_FALLBACK_CURRENCY = "USD"

# IBKR-Trade-Bodies tragen keine direkte `currency` mehr (Live-Recording
# AP-02 #04: Feld nur bei FX-Cash-Trades vorhanden). Stattdessen liefert
# das CP-Gateway `listing_exchange` (z.B. NYSE, IBIS, LSE). Diese Tabelle
# deckt die fuer U25235077 relevanten Boersen ab; alles Unbekannte
# faellt auf _FALLBACK_CURRENCY zurueck und markiert den Trade als
# currency_assumed=True, damit Konsumenten die Annahme erkennen.
_EXCHANGE_CURRENCY: dict[str, str] = {
    # USD
    "NYSE": "USD", "NASDAQ": "USD", "ARCA": "USD", "AMEX": "USD",
    "BATS": "USD", "IEX": "USD", "DARK": "USD", "ISLAND": "USD",
    "PINK": "USD", "PSE": "USD", "PHLX": "USD", "BYX": "USD",
    "EDGEA": "USD", "EDGEX": "USD", "MEMX": "USD",
    # EUR
    "IBIS": "EUR", "IBIS2": "EUR", "FWB": "EUR", "SWB": "EUR",
    "TRADEGATE": "EUR", "GETTEX": "EUR", "AEB": "EUR", "ENEXT.BE": "EUR",
    "SBF": "EUR", "BVME": "EUR", "BM": "EUR",
    # GBP
    "LSE": "GBP", "LSEETF": "GBP",
    # CHF
    "EBS": "CHF", "VIRTX": "CHF",
    # CAD
    "TSE": "CAD", "VENTURE": "CAD",
    # JPY
    "TSEJ": "JPY", "OSE.JPN": "JPY",
    # HKD
    "SEHK": "HKD", "SEHKNTL": "HKD",
    # AUD
    "ASX": "AUD",
    # SEK
    "SFB": "SEK",
}


def _currency_from_exchange(listing_exchange: str | None) -> str | None:
    if not listing_exchange:
        return None
    return _EXCHANGE_CURRENCY.get(listing_exchange.upper())


class Trade(BaseModel):
    execution_id: str
    order_id: str | None = None
    account_id: str | None = None
    conid: int | None = None
    symbol: str | None = None
    side: str | None = None
    quantity: str | None = None
    price: Money | None = None
    net_amount: Money | None = None
    commission: Money | None = None
    executed_at: datetime | None = None
    currency_assumed: bool = Field(
        default=False,
        description="True, falls die Commission-Currency mangels CP-Gateway-Info auf USD gesetzt wurde.",
    )


class TradesAggregate(BaseModel):
    metric: str
    period_from: date
    period_to: date
    account_id: str | None = None
    value: Money
    trade_count: int
    currency_assumption: str | None = Field(
        default=None,
        description="Wenn gesetzt: die Aggregation fasst Trades zusammen, deren Commission-Currency unbekannt war und auf diese Currency angenommen wurde.",
    )


class TradesService:
    def __init__(self, client: CPGatewayClient) -> None:
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
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="`to` muss >= `from` sein",
            )
        days = self._period_to_days(period_from, today)
        raw = await self._fetch_raw(days)
        trades = [_map_trade(entry) for entry in raw]
        return [
            t
            for t in trades
            if _within_window(t.executed_at, period_from, period_to)
            and (account_id is None or t.account_id is None or t.account_id == account_id)
        ]

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
                # Mehrere Currencies: in v0.10.0 explizit nicht summieren.
                # Aggregat liefert nur die Trades in der ersten gefundenen
                # Currency. Multi-Currency-Aggregation bleibt Folgekarte.
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

    # ---- Helfer ----

    def _period_to_days(self, period_from: date, today: date | None) -> int:
        ref = today or datetime.now(timezone.utc).date()
        delta = (ref - period_from).days + 1
        if delta < _MIN_DAYS:
            return _MIN_DAYS
        if delta > _MAX_DAYS:
            return _MAX_DAYS
        return delta

    async def _fetch_raw(self, days: int) -> list[dict[str, Any]]:
        response = await self._client.get(
            "/iserver/account/trades", params={"days": days}
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"CP-Gateway-Fehler bei trades: HTTP {response.status_code}",
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="CP-Gateway lieferte unerwartetes Schema bei trades",
            )
        return [entry for entry in payload if isinstance(entry, dict)]


def _map_trade(entry: dict[str, Any]) -> Trade:
    # IBKR-Live-Schema (Recording AP-02 #04): Trade-Body hat `account`
    # (nicht `account_id`) und keine direkte `currency`. Currency wird
    # aus `listing_exchange` abgeleitet; Legacy-Felder bleiben als
    # Fallback fuer den hartcodierten Mock und fuer FX-Cash-Trades, in
    # denen IBKR `currency` setzt.
    currency_raw = (
        entry.get("currency")
        or entry.get("base_currency")
        or _currency_from_exchange(entry.get("listing_exchange"))
    )
    price_currency = currency_raw or _FALLBACK_CURRENCY
    commission_currency = currency_raw or _FALLBACK_CURRENCY
    currency_assumed = currency_raw is None

    price_raw = entry.get("price")
    quantity_raw = entry.get("size") or entry.get("quantity")
    quantity_str = str(quantity_raw) if quantity_raw is not None else None
    price_money = normalize_money(price_raw, price_currency)

    net_amount_money = normalize_money(entry.get("net_amount"), price_currency)
    if net_amount_money is None and price_raw is not None and quantity_raw is not None:
        try:
            net_amount_money = normalize_money(
                Decimal(str(price_raw)) * Decimal(str(quantity_raw)),
                price_currency,
            )
        except Exception:  # nosec - bestmoegliche Ableitung
            net_amount_money = None

    commission_money = normalize_money(entry.get("commission"), commission_currency)

    executed_raw = entry.get("trade_time") or entry.get("executed_at")
    executed_at = _parse_datetime(executed_raw)

    return Trade(
        execution_id=str(entry.get("execution_id") or entry.get("trade_id") or ""),
        order_id=_str_or_none(entry.get("order_id")),
        account_id=_str_or_none(
            entry.get("account")
            or entry.get("accountCode")
            or entry.get("account_id")
            or entry.get("acctId")
        ),
        conid=int(entry["conid"]) if entry.get("conid") is not None else None,
        symbol=_str_or_none(entry.get("symbol")),
        side=_str_or_none(entry.get("side")),
        quantity=quantity_str,
        price=price_money,
        net_amount=net_amount_money,
        commission=commission_money,
        executed_at=executed_at,
        currency_assumed=currency_assumed and (commission_money is not None or price_money is not None),
    )


def _parse_datetime(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def _within_window(executed_at: datetime | None, period_from: date, period_to: date) -> bool:
    if executed_at is None:
        return True
    d = executed_at.date()
    return period_from <= d <= period_to


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


__all__ = ["Trade", "TradesAggregate", "TradesService"]
