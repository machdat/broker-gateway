"""Aktion-Layer fuer Paper-Tests (AP-07 K5).

Wrapper um die Schreib-Endpunkte des broker-gateway-paper-Stacks
(``POST /v1/orders``, ``DELETE /v1/orders/{id}``,
``GET /v1/orders/{id}``, ``GET /v1/quotes/snapshot``,
``GET /v1/quotes/stream``). Vor jeder Schreib-Operation laeuft der
``safety``-Layer (Account-Whitelist + Notional-Limits +
Kill-Switch).

Klasse ``PaperActions`` haelt einen ``httpx.AsyncClient`` und
einen kumulativen Notional-Counter pro Test-Lauf. Alle Methoden sind
async.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import httpx

from tests_paper._dsl.safety import (
    PaperSafetyError,
    assert_paper_account,
    assert_within_paper_limits,
)


class PaperActions:
    """Sicherer DSL-Wrapper um die Order-/Quote-Endpunkte."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        account_id: str,
    ) -> None:
        self._client = client
        # Account wird bereits beim Init gegen die Paper-Whitelist
        # geprueft - so faellt eine fehlerhafte ENV sofort auf, nicht
        # erst bei der ersten place_order-Operation.
        self._account_id = assert_paper_account(account_id)
        self._cumulative_notional = Decimal("0")

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def cumulative_notional_usd(self) -> Decimal:
        return self._cumulative_notional

    # ------------------------------------------------------------
    # Read-only
    # ------------------------------------------------------------

    async def snapshot(
        self,
        *,
        conids: list[int],
        fields: list[str] | None = None,
    ) -> httpx.Response:
        params = {"conids": ",".join(str(c) for c in conids)}
        if fields:
            params["fields"] = ",".join(fields)
        return await self._client.get("/v1/quotes/snapshot", params=params)

    async def get_order(self, order_id: int | str) -> httpx.Response:
        return await self._client.get(f"/v1/orders/{order_id}")

    async def get_trades(self) -> httpx.Response:
        return await self._client.get("/v1/trades")

    async def get_portfolio_summary(self) -> httpx.Response:
        return await self._client.get(
            f"/v1/portfolio/{self._account_id}/summary"
        )

    # ------------------------------------------------------------
    # Schreib-Operationen mit Safety-Vorlauf
    # ------------------------------------------------------------

    async def place_order(
        self,
        *,
        conid: int,
        side: str,
        quantity: Decimal | int | str,
        limit_price: Decimal | int | str,
        time_in_force: str = "DAY",
    ) -> httpx.Response:
        """Limit-Order platzieren - mit Notional-Limit-Check."""
        if os.environ.get("BG_PAPER_TESTS_DISABLED", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            raise PaperSafetyError(
                "BG_PAPER_TESTS_DISABLED gesetzt - Schreib-Operation "
                "wird abgelehnt."
            )
        notional = Decimal(str(quantity)) * Decimal(str(limit_price))
        cumulative = self._cumulative_notional + notional
        assert_within_paper_limits(
            notional_usd=notional,
            cumulative_notional_usd=cumulative,
        )
        body: dict[str, Any] = {
            "account": self._account_id,
            "conid": conid,
            "side": side.upper(),
            "quantity": str(quantity),
            "order_type": "LMT",
            "limit_price": str(limit_price),
            "time_in_force": time_in_force.upper(),
        }
        response = await self._client.post("/v1/orders", json=body)
        # Erst nach erfolgreichem 2xx den Counter erhoehen - sonst zaehlen
        # wir 4xx-Bodies in das Limit, was zu falschen Drosselungen fuehrt.
        if 200 <= response.status_code < 300:
            self._cumulative_notional = cumulative
        return response

    async def cancel_order(self, order_id: int | str) -> httpx.Response:
        return await self._client.delete(f"/v1/orders/{order_id}")

    async def flatten_position(self, *, conid: int) -> httpx.Response:
        """Schliesst eine Position via Market-Order in Gegenrichtung.

        Vereinfacht: prueft die aktuelle Position und gibt eine
        passende Sell/Buy-Quantity zurueck. Wenn keine Position
        vorhanden, wird ``204 No Content`` simuliert (kein REST-Call).
        """
        response = await self._client.get(
            f"/v1/portfolio/{self._account_id}/positions/{conid}"
        )
        if response.status_code == 404:
            return response  # nichts zu schliessen
        response.raise_for_status()
        body = response.json()
        position = Decimal(str(body.get("position", 0)))
        if position == 0:
            return response
        side = "SELL" if position > 0 else "BUY"
        quantity = abs(position)
        # Market-Order via place_order mit limit_price=Marktpreis aus
        # der Position (vorhanden) - hier vereinfacht auf 0 als
        # Platzhalter; der reale flatten-Flow ueber das CP-Gateway
        # nimmt Last/Bid/Ask. Diese Karte legt nur das Skelett fest.
        last_price = body.get("last") or body.get("avg_price") or "0"
        return await self.place_order(
            conid=conid,
            side=side,
            quantity=quantity,
            limit_price=last_price,
            time_in_force="DAY",
        )


__all__ = ["PaperActions"]
