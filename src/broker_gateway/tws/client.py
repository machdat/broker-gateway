"""Async-Adapter fuer die IBKR TWS-API ueber ib_async.

Pendant zu :class:`broker_gateway.cp.client.CPGatewayClient`, aber
gegen die TWS-Socket-API. Read-Only-Pfade.

Disziplin (Memory ``project_tws_api_pi5_setup``):

- Alle Top-Level-Methoden sind ``async`` und nutzen die ``*Async``-
  Varianten von ``ib_async``. Sync-Wrapper wuerden im FastAPI-Async-
  Kontext einen ueberlappenden Loop ausloesen.
- Jede ``TWSClient``-Instanz reserviert eine ``clientId`` aus einem
  Pool (siehe :class:`ClientIdPool`). Default-Pool 100..199, da 0 als
  TWS-Master reserviert ist und 1..99 fuer manuelle TWS-Sessions im
  Projekt freigehalten werden.
- ``read_only`` ist auf ``True`` defaultet. Order-Routing kommt in
  einer Folge-Karte mit ``read_only=False``.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from broker_gateway.tws.types import (
    AccountField,
    Bar,
    Position,
    Snapshot,
    Tick,
)

if TYPE_CHECKING:
    from ib_async.contract import Contract


_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PAPER_PORT = 4002
_DEFAULT_LIVE_PORT = 4001
_DEFAULT_CLIENT_ID_RANGE: tuple[int, int] = (100, 199)
_DEFAULT_CONNECT_TIMEOUT_S = 20.0
_SNAPSHOT_TIMEOUT_S = 10.0
_STREAM_POLL_INTERVAL_S = 1.0


class ContractNotFoundError(LookupError):
    """Geworfen, wenn ``qualifyContractsAsync`` keinen Treffer liefert."""


class ClientIdPool:
    """AsyncQueue-basierter Pool fuer TWS-API ``clientId``-Werte.

    IB Gateway weist jeder Verbindung ein ``clientId`` zu. Mehrere
    parallele Connects mit derselben ID kicken die aeltere Session
    raus. Der Pool stellt sicher, dass parallele
    :class:`TWSClient`-Instanzen (Live + Paper, mehrere Adapter,
    Stream + Snapshot) eindeutige IDs ziehen.

    Default-Range 100..199 (siehe Memory ``project_tws_api_pi5_setup``):

    - 0 : TWS-Master, reserviert.
    - 1..99 : manuelle TWS-/Spike-Sessions, nicht ueberbuchen.
    - 100..199 : broker-gateway-Adapter.
    - 200+ : frei fuer Folge-Projekte (PSM-Kurator usw.).
    """

    def __init__(self, range_: tuple[int, int] = _DEFAULT_CLIENT_ID_RANGE) -> None:
        start, end = range_
        if start <= 0 or end < start:
            raise ValueError(f"Invalid clientId range: {range_!r}")
        self._range = range_
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        for cid in range(start, end + 1):
            self._queue.put_nowait(cid)

    @property
    def range(self) -> tuple[int, int]:
        return self._range

    def available(self) -> int:
        return self._queue.qsize()

    async def acquire(self) -> int:
        return await self._queue.get()

    def release(self, client_id: int) -> None:
        start, end = self._range
        if not start <= client_id <= end:
            raise ValueError(
                f"clientId {client_id} ausserhalb des Pool-Bereichs {self._range}"
            )
        self._queue.put_nowait(client_id)


class TWSClient:
    """Async-Adapter fuer die IBKR TWS-API.

    Read-Only-Pfade:

    - :meth:`account_summary` - Account-Felder (NetLiquidation, BuyingPower, ...).
    - :meth:`positions` - offene Positionen pro Account.
    - :meth:`qualify` - Contract-Aufloesung (conId, primaryExchange).
    - :meth:`historical_bars` - historische Bars (1D/1h/...).
    - :meth:`market_snapshot` - Snapshot (delayed-frozen falls keine Live-Sub).
    - :meth:`market_stream` - kontinuierliche Tick-Updates (AsyncIterator).

    Lebenszyklus::

        pool = ClientIdPool()
        async with TWSClient(client_id_pool=pool, paper=True) as client:
            rows = await client.account_summary()
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        client_id_pool: ClientIdPool | None = None,
        *,
        paper: bool = True,
        read_only: bool = True,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_S,
        ib: Any | None = None,
    ) -> None:
        if ib is None:
            from ib_async import IB

            ib = IB()
        self._ib = ib
        self._host = host or os.environ.get("BG_TWS_HOST") or _DEFAULT_HOST
        self._port = port or int(
            os.environ.get("BG_TWS_PORT")
            or (_DEFAULT_PAPER_PORT if paper else _DEFAULT_LIVE_PORT)
        )
        self._pool = client_id_pool or ClientIdPool()
        self._client_id: int | None = None
        self._paper = paper
        self._read_only = read_only
        self._connect_timeout = connect_timeout

    # ---- Async-Context-Manager ----

    async def __aenter__(self) -> TWSClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.disconnect()

    # ---- Properties ----

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def paper(self) -> bool:
        return self._paper

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def client_id(self) -> int | None:
        return self._client_id

    # ---- Lifecycle ----

    async def connect(self) -> None:
        if self._client_id is not None:
            return
        client_id = await self._pool.acquire()
        try:
            await self._ib.connectAsync(
                self._host,
                self._port,
                clientId=client_id,
                timeout=self._connect_timeout,
                readonly=self._read_only,
            )
        except BaseException:
            self._pool.release(client_id)
            raise
        self._client_id = client_id

    async def disconnect(self) -> None:
        try:
            self._ib.disconnect()
        finally:
            if self._client_id is not None:
                self._pool.release(self._client_id)
                self._client_id = None

    def is_connected(self) -> bool:
        return bool(self._ib.isConnected())

    # ---- Read-Pfade ----

    async def account_summary(
        self, account: str | None = None
    ) -> dict[str, AccountField]:
        """Account-Felder (NetLiquidation, BuyingPower, ...).

        Bei mehreren Accounts ohne Filter koennen ``tag``-Schluessel
        kollidieren - der letzte Eintrag gewinnt. Fuer deterministische
        Ergebnisse ``account`` setzen.
        """
        rows = await self._ib.accountSummaryAsync(account or "")
        return {row.tag: AccountField.from_account_value(row) for row in rows}

    async def positions(self, account: str | None = None) -> list[Position]:
        """Offene Positionen.

        Nutzt :meth:`ib_async.IB.reqAccountUpdatesAsync` +
        :meth:`ib_async.IB.portfolio`, weil PortfolioItem auch
        MarketPrice/MarketValue/PnL liefert. Die schmale
        ``reqPositionsAsync``-Variante haette nur Lots+AvgCost.
        """
        target = account or ""
        await self._ib.reqAccountUpdatesAsync(target)
        items = self._ib.portfolio()
        if account:
            items = [it for it in items if it.account == account]
        return [Position.from_portfolio_item(it) for it in items]

    async def qualify(self, contract: Contract) -> Contract:
        """Aufloesung von ``conId`` und ``primaryExchange``.

        Wirft :class:`ContractNotFoundError`, wenn IBKR keinen Treffer
        liefert (typisch bei Tippfehlern, abgelaufenen Optionen,
        delisteten Symbolen).
        """
        results = await self._ib.qualifyContractsAsync(contract)
        if not results or results[0] is None:
            raise ContractNotFoundError(
                f"Kein qualifizierter Contract fuer {contract!r}"
            )
        first = results[0]
        if isinstance(first, list):
            if not first or first[0] is None:
                raise ContractNotFoundError(
                    f"Kein qualifizierter Contract fuer {contract!r}"
                )
            return first[0]
        return first

    async def historical_bars(
        self,
        contract: Contract,
        *,
        end_date_time: str = "",
        duration: str = "1 D",
        bar_size: str = "1 hour",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> list[Bar]:
        """Historische Bars (UTC-normalisiert)."""
        bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end_date_time,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
        )
        return [Bar.from_bar_data(b) for b in bars]

    async def market_snapshot(
        self,
        contract: Contract,
        *,
        market_data_type: int = 3,
        timeout: float = _SNAPSHOT_TIMEOUT_S,
    ) -> Snapshot:
        """Snapshot (last/bid/ask/volume).

        Defaultet auf ``market_data_type=3`` (delayed). Live-Subscription
        muss vom Caller gewaehlt werden, sonst sind Subscription-
        Konflikte (Error 10197) wahrscheinlich. Siehe Memory
        ``project_tws_api_pi5_setup`` Marketdata-Quirks.

        Wartet bis zum ersten ``updateEvent`` oder ``timeout`` Sekunden
        - was zuerst kommt. ``snapshot=True`` triggert auf IBKR-Seite
        einen Auto-Cancel nach Erfolg, ein expliziter Cancel hier ist
        nicht noetig.
        """
        self._ib.reqMarketDataType(market_data_type)
        ticker = self._ib.reqMktData(
            contract, "", snapshot=True, regulatorySnapshot=False
        )
        loop = asyncio.get_running_loop()
        first_update: asyncio.Future[None] = loop.create_future()

        def _on_update(_: Any) -> None:
            if not first_update.done():
                first_update.set_result(None)

        ticker.updateEvent += _on_update
        try:
            try:
                await asyncio.wait_for(first_update, timeout=timeout)
            except TimeoutError:
                # Snapshot kam nicht zurueck - geben wir trotzdem den
                # Ticker-Stand zurueck, damit der Caller selbst
                # entscheiden kann (alle Felder None / Decimal None).
                pass
        finally:
            ticker.updateEvent -= _on_update
        return Snapshot.from_ticker(ticker)

    async def market_stream(
        self,
        contract: Contract,
        *,
        market_data_type: int = 3,
        field: str = "last",
        poll_interval: float = _STREAM_POLL_INTERVAL_S,
    ) -> AsyncIterator[Tick]:
        """Kontinuierlicher Tick-Stream ueber ``ticker.updateEvent``.

        ``async for tick in client.market_stream(contract):`` yieldet
        einen :class:`Tick` pro IBKR-Update. Der Stream endet, wenn
        :meth:`disconnect` gerufen wird oder die Connection abreisst.

        ``poll_interval`` begrenzt das Blocking pro Schleifendurchlauf,
        damit ein Disconnect zeitnah erkannt wird. Default 1 s.
        """
        self._ib.reqMarketDataType(market_data_type)
        ticker = self._ib.reqMktData(
            contract, "", snapshot=False, regulatorySnapshot=False
        )
        queue: asyncio.Queue[Tick] = asyncio.Queue()

        def _on_update(t: Any) -> None:
            queue.put_nowait(Tick.from_ticker(t, field=field))

        ticker.updateEvent += _on_update
        try:
            while self.is_connected():
                try:
                    tick = await asyncio.wait_for(
                        queue.get(), timeout=poll_interval
                    )
                except TimeoutError:
                    continue
                yield tick
        finally:
            ticker.updateEvent -= _on_update
            try:
                self._ib.cancelMktData(contract)
            except Exception:
                # cancel kann fehlschlagen, wenn die Connection zu
                # diesem Zeitpunkt schon weg ist. Das ist ein
                # Cleanup-Pfad - nicht als Fehler eskalieren.
                pass
