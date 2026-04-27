"""Portfolio-Adapter: Holdings, Positions, Ledger gegen das CP-Gateway.

TTL-Cache pro account_id. Invalidate-Hook wird von Order-Lifecycle
(Karte 09) aufgerufen, sobald sich der Bestand veraendert hat.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from broker_gateway.cache import TTLCache
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.money import Money, normalize_money, normalize_summary_money


_DEFAULT_TTL_S = 30.0
_POSITIONS_PAGE_SIZE = 30
_POSITIONS_MAX_PAGES = 50


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
        positions: list[Position] = []
        for page_id in range(_POSITIONS_MAX_PAGES):
            response = await self._client.get(
                f"/portfolio/{account_id}/positions/{page_id}"
            )
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
            page = [_map_position(account_id, entry) for entry in payload if "conid" in entry]
            positions.extend(page)
            if len(payload) < _POSITIONS_PAGE_SIZE:
                break
        self._positions_cache.set(account_id, positions)
        return positions

    async def ledger(self, account_id: str) -> Ledger:
        cached, hit = self._ledger_cache.get(account_id)
        if hit and cached is not None:
            return cached
        response = await self._client.get(f"/portfolio/{account_id}/ledger")
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
            # IBKR liefert neben den Currency-Keys (USD, EUR, ...) auch
            # einen "BASE"-Aggregat-Eintrag. Der wird nicht als separate
            # Currency aufgenommen - die echte Currency steht im Body.
            if ccy_key.upper() == "BASE":
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

        response = await self._client.get(f"/portfolio/{account_id}/summary")
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"CP-Gateway-Fehler bei summary: HTTP {response.status_code}",
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="CP-Gateway lieferte unerwartetes Schema bei summary",
            )

        net_liquidation = normalize_summary_money(payload.get("netliquidation"))
        cash_total = normalize_summary_money(payload.get("totalcashvalue"))
        positions_value = normalize_summary_money(payload.get("grosspositionvalue"))
        base_currency = (
            net_liquidation.currency
            if net_liquidation is not None
            else (cash_total.currency if cash_total is not None else None)
        )

        # position_count kommt nicht aus dem nativen Summary - aus positions
        # ableiten, mit Fallback 0, falls positions selbst fehlschlaegt.
        positions = await self.positions(account_id)

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


