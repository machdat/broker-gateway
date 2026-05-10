"""Portfolio-Adapter gegen die TWS-Socket-API (ib_async).

Pendant zu :class:`broker_gateway.cp.portfolio.PortfolioService`, aber gegen
die TWS-API statt cpgateway. Schema-Identitaet: liefert dieselben Pydantic-
Modelle (Position, Ledger, LedgerEntry, PortfolioSummary), damit die HTTP-
API-Konsumenten beim Backend-Switch keine Drift sehen.

Folge-Karte zu 33cb35b1 (TWSLifecycle): die Service-Schicht macht den
Cutover komplett, der dort begonnen wurde. AP ``2a203c58-...`` Phase 1.

Subscribe-Strategie: pro Service-Instanz wird jeder Account genau einmal
abonniert; Folgecalls lesen synchron aus ``ib.portfolio()`` /
``ib.accountValues(account_id)`` - ib_async pflegt den Cache via
``updatePortfolio``-Events. Der Initial-Subscribe lauft ueber
``ib.reqAccountUpdatesAsync(...)`` mit hartem ``asyncio.wait_for``-
Timeout: die Coroutine resolvet zuverlaessig nur beim allerersten
``accountDownloadEnd``-Trigger pro Account und haengt sonst indefinit
(Memory ``project_tws_portfolio_resubscribe_hang``, v2.1.0 Live-Bug).
Wenn der Lifespan-``connectAsync`` den Account schon initialisiert hat
(Log: ``Synchronization complete``), greift der Timeout - der Cache ist
trotzdem frisch und wir markieren den Account als ``subscribed``. Die
**synchrone** ``ib.reqAccountUpdates`` ist im FastAPI-async-Kontext
**nicht** nutzbar: sie ruft intern ``loop.run_until_complete`` und
crasht mit ``RuntimeError: this event loop is already running``
(v2.1.1/v2.1.2 Live-Bugs). ``invalidate(...)`` ist ein no-op fuer
Schema-Kompatibilitaet mit cp.portfolio.PortfolioService (der Order-
Lifecycle ruft das nach Bestand-Aenderungen).
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from broker_gateway.cp.portfolio import (
    Ledger,
    LedgerEntry,
    PortfolioSummary,
    Position,
)
from broker_gateway.money import Money, normalize_money

if TYPE_CHECKING:
    from broker_gateway.tws.client import TWSClient


logger = logging.getLogger(__name__)

_SUBSCRIBE_TIMEOUT_S = 2.0


__all__ = [
    "TWSPortfolioService",
    "Position",
    "Ledger",
    "LedgerEntry",
    "PortfolioSummary",
]


class TWSPortfolioService:
    """Read-Pfad fuer Portfolio-Daten via ib_async.

    Liefert dieselben Pydantic-Modelle wie ``cp.portfolio.PortfolioService``,
    damit der Backend-Switch fuer Konsumenten transparent ist.
    """

    def __init__(self, client: TWSClient) -> None:
        self._client = client
        self._subscribed_accounts: set[str] = set()
        self._subscribe_lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        # Schema-Kompatibilitaet zu cp.portfolio.PortfolioService.ttl_seconds.
        # Im TWS-Pfad steuern ib_async-Events die Frische, ein TTL gibt es
        # nicht - der Wert ist nur informativ.
        return 0.0

    def invalidate(self, account_id: str) -> None:
        """No-op. ib_async-Events steuern Aktualitaet."""
        return None

    async def positions(self, account_id: str) -> list[Position]:
        items = await self._fetch_portfolio_items(account_id)
        results: list[Position] = []
        for item in items:
            currency = (getattr(item.contract, "currency", None) or None)
            quantity = str(Decimal(str(item.position)))
            results.append(
                Position(
                    account_id=item.account,
                    conid=int(item.contract.conId),
                    quantity=quantity,
                    avg_cost=normalize_money(item.averageCost, currency),
                    market_price=normalize_money(item.marketPrice, currency),
                    market_value=normalize_money(item.marketValue, currency),
                )
            )
        return results

    async def ledger(self, account_id: str) -> Ledger:
        values = await self._fetch_account_values(account_id)
        per_currency: dict[str, dict[str, Money | None]] = {}
        for av in values:
            currency = (getattr(av, "currency", "") or "").upper()
            if not currency or currency == "BASE":
                continue
            tag = getattr(av, "tag", "")
            slot = per_currency.setdefault(currency, {})
            if tag == "CashBalance":
                slot["cash_balance"] = normalize_money(av.value, currency)
            elif tag == "SettledCash" and "settled_cash" not in slot:
                slot["settled_cash"] = normalize_money(av.value, currency)
        entries = [
            LedgerEntry(
                currency=ccy,
                cash_balance=fields.get("cash_balance"),
                settled_cash=fields.get("settled_cash"),
            )
            for ccy, fields in sorted(per_currency.items())
        ]
        return Ledger(account_id=account_id, entries=entries)

    async def summary(self, account_id: str) -> PortfolioSummary:
        values = await self._fetch_account_values(account_id)
        net_liquidation: Money | None = None
        cash_total: Money | None = None
        positions_value: Money | None = None
        base_currency: str | None = None
        for av in values:
            tag = getattr(av, "tag", "")
            currency = (getattr(av, "currency", "") or "").upper() or None
            if not currency or currency == "BASE":
                continue
            if tag == "NetLiquidation" and net_liquidation is None:
                net_liquidation = normalize_money(av.value, currency)
                base_currency = base_currency or currency
            elif tag == "TotalCashValue" and cash_total is None:
                cash_total = normalize_money(av.value, currency)
                base_currency = base_currency or currency
            elif tag == "GrossPositionValue" and positions_value is None:
                positions_value = normalize_money(av.value, currency)
                base_currency = base_currency or currency
        positions = await self.positions(account_id)
        return PortfolioSummary(
            account_id=account_id,
            base_currency=base_currency,
            cash_total=cash_total,
            positions_value=positions_value,
            net_liquidation=net_liquidation,
            position_count=len(positions),
        )

    # ---- Helpers -----------------------------------------------------

    async def _ensure_subscribed(self, account_id: str) -> None:
        # Subscribe genau einmal pro Account-Id. Double-Checked-Locking,
        # damit parallele HTTP-Calls den ersten Subscribe nicht doppelt
        # senden. Bei leerem account_id (Wildcard-Lese ohne Filter) gibt
        # es nichts zu subscriben - ib.portfolio() liefert dann den
        # primaer-Account-Cache, der via connectAsync ohnehin schon da
        # ist.
        if not account_id:
            return
        if account_id in self._subscribed_accounts:
            return
        async with self._subscribe_lock:
            if account_id in self._subscribed_accounts:
                return
            ib = self._client._ib  # noqa: SLF001 - bewusste Low-Level-Bruecke
            req_async = getattr(ib, "reqAccountUpdatesAsync", None)
            if callable(req_async):
                # Hartes Timeout: die Coroutine resolvet nur beim ersten
                # accountDownloadEnd-Trigger pro Account. Wenn der
                # Lifespan-connectAsync den Account schon initialisiert
                # hat, kommt der Trigger nicht erneut und der await
                # haengt indefinit. Im TimeoutError ist der ib_async-
                # Cache trotzdem aktuell (updatePortfolio-Events), und
                # der Subscribe-Cache markiert den Account als bekannt -
                # Folgecalls lesen synchron weiter.
                try:
                    await asyncio.wait_for(
                        req_async(account_id),
                        timeout=_SUBSCRIBE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        "reqAccountUpdatesAsync(%s) timeoutet nach %.1fs - "
                        "Lifespan-connectAsync hat den Account bereits "
                        "initialisiert, Cache ist via updatePortfolio-"
                        "Events frisch",
                        account_id,
                        _SUBSCRIBE_TIMEOUT_S,
                    )
            self._subscribed_accounts.add(account_id)

    async def _fetch_portfolio_items(self, account_id: str) -> list[Any]:
        await self._ensure_subscribed(account_id)
        ib = self._client._ib  # noqa: SLF001
        items = ib.portfolio()
        if account_id:
            return [it for it in items if getattr(it, "account", "") == account_id]
        return list(items)

    async def _fetch_account_values(self, account_id: str) -> list[Any]:
        await self._ensure_subscribed(account_id)
        ib = self._client._ib  # noqa: SLF001
        # ib_async.IB.accountValues(account) filtert serverseitig; bei manchen
        # Mock-Implementierungen ist das Argument optional. Fallback auf
        # client-seitiges Filtern.
        try:
            values = ib.accountValues(account_id)
        except TypeError:
            values = ib.accountValues()
            values = [
                av for av in values
                if not getattr(av, "account", "")
                or getattr(av, "account", "") == account_id
            ]
        return list(values)
