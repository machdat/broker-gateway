"""Portfolio-Adapter: Holdings, Positions, Ledger gegen das CP-Gateway.

TTL-Cache pro account_id. Invalidate-Hook wird von Order-Lifecycle
(Karte 09) aufgerufen, sobald sich der Bestand veraendert hat.
"""
from __future__ import annotations

import os
from collections import defaultdict
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from broker_gateway.cache import TTLCache
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.money import Money, normalize_money


_DEFAULT_TTL_S = 30.0


def _ttl_from_env() -> float:
    raw = os.environ.get("BG_PORTFOLIO_TTL_S")
    if not raw:
        return _DEFAULT_TTL_S
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_TTL_S


class Position(BaseModel):
    account_id: str
    conid: int
    quantity: str = Field(description="Stuckzahl als Decimal-String (Bruchstuecke moeglich)")
    avg_cost: Money | None = None
    market_price: Money | None = None
    market_value: Money | None = None


class LedgerEntry(BaseModel):
    currency: str
    cash_balance: Money | None = None
    settled_cash: Money | None = None


class Ledger(BaseModel):
    account_id: str
    entries: list[LedgerEntry]


class PortfolioSummary(BaseModel):
    account_id: str
    base_currency: str | None = None
    cash_total: Money | None = None
    positions_value: Money | None = None
    net_liquidation: Money | None = None
    position_count: int = 0


class PortfolioService:
    def __init__(
        self,
        client: CPGatewayClient,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        self._client = client
        ttl = ttl_seconds if ttl_seconds is not None else _ttl_from_env()
        self._summary_cache: TTLCache[str, PortfolioSummary] = TTLCache(ttl)
        self._positions_cache: TTLCache[str, list[Position]] = TTLCache(ttl)
        self._ledger_cache: TTLCache[str, Ledger] = TTLCache(ttl)

    @property
    def ttl_seconds(self) -> float:
        return self._summary_cache.ttl_seconds

    # ---- Cache-Bust-Hook (Karte 09 ruft das nach Order-Aenderungen) ----
    def invalidate(self, account_id: str) -> None:
        self._summary_cache.invalidate(account_id)
        self._positions_cache.invalidate(account_id)
        self._ledger_cache.invalidate(account_id)

    # ---- Endpunkt-Logik ----

    async def positions(self, account_id: str) -> list[Position]:
        cached, hit = self._positions_cache.get(account_id)
        if hit:
            return cached or []
        response = await self._client.get(f"/iserver/account/{account_id}/positions")
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"CP-Gateway-Fehler bei positions: HTTP {response.status_code}",
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="CP-Gateway lieferte unerwartetes Schema bei positions",
            )
        positions = [_map_position(account_id, entry) for entry in payload if "conid" in entry]
        self._positions_cache.set(account_id, positions)
        return positions

    async def ledger(self, account_id: str) -> Ledger:
        cached, hit = self._ledger_cache.get(account_id)
        if hit and cached is not None:
            return cached
        response = await self._client.get(f"/iserver/account/{account_id}/ledger")
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"CP-Gateway-Fehler bei ledger: HTTP {response.status_code}",
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="CP-Gateway lieferte unerwartetes Schema bei ledger",
            )
        entries: list[LedgerEntry] = []
        for ccy_key, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            currency = (entry.get("currency") or ccy_key).upper()
            entries.append(
                LedgerEntry(
                    currency=currency,
                    cash_balance=normalize_money(entry.get("cashbalance"), currency),
                    settled_cash=normalize_money(entry.get("settledcash"), currency),
                )
            )
        ledger = Ledger(account_id=account_id, entries=entries)
        self._ledger_cache.set(account_id, ledger)
        return ledger

    async def summary(self, account_id: str) -> PortfolioSummary:
        cached, hit = self._summary_cache.get(account_id)
        if hit and cached is not None:
            return cached

        positions = await self.positions(account_id)
        ledger = await self.ledger(account_id)

        # Aggregat-Bildung. Heuristik fuer base_currency: erste Ledger-Entry,
        # falls vorhanden. Net-Liquidation = cash_total + positions_value,
        # alles in derselben Currency. Liegen Positionen in mehreren Waehrungen
        # vor, wird positions_value pro Currency aggregiert und nur die
        # Position-Summe in der base_currency in net_liquidation einberechnet
        # - andere Currencies muessen vom Caller separat gewuerdigt werden.
        base_currency = ledger.entries[0].currency if ledger.entries else None
        cash_total = _sum_money_for_currency(
            (entry.cash_balance for entry in ledger.entries),
            base_currency,
        )
        positions_value = _sum_money_for_currency(
            (pos.market_value for pos in positions),
            base_currency,
        )
        net_liquidation = _add_money(cash_total, positions_value)

        summary = PortfolioSummary(
            account_id=account_id,
            base_currency=base_currency,
            cash_total=cash_total,
            positions_value=positions_value,
            net_liquidation=net_liquidation,
            position_count=len(positions),
        )
        self._summary_cache.set(account_id, summary)
        return summary


def _map_position(account_id: str, entry: dict[str, Any]) -> Position:
    currency = entry.get("currency")
    quantity_raw = entry.get("position", 0)
    quantity = str(Decimal(str(quantity_raw)))

    avg_cost = normalize_money(entry.get("avgCost"), currency)
    market_price = normalize_money(entry.get("mktPrice"), currency)
    market_value = normalize_money(entry.get("mktValue"), currency)
    if market_value is None and market_price is not None:
        # Manche CP-Gateway-Antworten liefern nur mktPrice; mktValue ableiten.
        try:
            qty = Decimal(quantity)
            mkt = Decimal(market_price.value)
            market_value = normalize_money(qty * mkt, currency)
        except Exception:  # nosec - bestmoegliche Ableitung; bei Fehlschlag bleibt None
            market_value = None

    return Position(
        account_id=str(entry.get("acctId") or account_id),
        conid=int(entry["conid"]),
        quantity=quantity,
        avg_cost=avg_cost,
        market_price=market_price,
        market_value=market_value,
    )


def _sum_money_for_currency(values, currency: str | None) -> Money | None:
    if currency is None:
        return None
    total = Decimal("0")
    seen = False
    for money in values:
        if money is None or money.currency != currency:
            continue
        total += Decimal(money.value)
        seen = True
    if not seen:
        return None
    return Money(value=str(total), currency=currency)


def _add_money(a: Money | None, b: Money | None) -> Money | None:
    if a is None and b is None:
        return None
    if a is None:
        return b
    if b is None:
        return a
    if a.currency != b.currency:
        return None
    total = Decimal(a.value) + Decimal(b.value)
    return Money(value=str(total), currency=a.currency)
