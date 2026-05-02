"""Topic-Adapter fuer IBKR ``smd`` (Single-Market-Data) Frames.

Aufgabe: rohe ``smd+<conid>``-Frames vom Client-Portal-WebSocket
in semantisch normierte, voll-snapshot-orientierte ``SmdFrame``-Objekte
uebersetzen.

Verhalten:

- **Mixed-Type-Dekodierung.** IBKR liefert Preise als Strings (``"271.55"``),
  Prozent-Change als Float, Sizes mal als String mal als int. Der Adapter
  konvertiert pro Feld auf den Anhang-B-Zieltyp (Decimal fuer Preise,
  int fuer Sizes, float fuer change_pct, str fuer Codes).
- **Delta-zu-Snapshot-Merge.** Erster Frame nach Subscribe enthaelt alle
  Felder, Folge-Frames nur Delta-Felder. Der Adapter haelt pro ``conid``
  einen internen Snapshot-State und merged neue Felder hinein, sodass
  am Egress immer ein Voll-Snapshot ankommt.
- **Dedup via (conid, _updated).** ``tic``-Multiplikator und Doppelversand
  liefern identische Frames mit gleichem ``_updated``. Der Adapter
  liefert in dem Fall ``None``.
- **Forward-Compat.** Unbekannte Field-IDs werden ignoriert.

Bewusst NICHT in K1:

- ``is_tradeable_now`` / ``current_session`` (kommen in K5 ueber den
  ``CalendarService``-Lookup).
- ``exchange_id`` (kommt in K4 ueber den ``Symbol→Boerse``-Lookup).

Die Felder existieren als ``Optional`` im Frame-Schema, sind in dieser
Karte aber konstant ``None``.

Layer-Trennung: kein Import von ``httpx`` oder dem CP-REST-Client. Der
Adapter ist eine reine Frame-Transformation.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from broker_gateway.cp.calendar import CalendarService, ExchangeCalendar
from broker_gateway.cp.tradeability import derive_tradeability


logger = logging.getLogger(__name__)


# Lookups, die der Adapter optional vom WSPushSource bekommt:
# Conid -> exchange_id (fuer den Calendar-Lookup), und ein Clock-Hook
# fuer Tests (Default: ``datetime.now(timezone.utc)``).
ConidToExchange = Callable[[int], Awaitable[str | None]]
ClockHook = Callable[[], datetime]


# Field-ID -> Adapter-Feldname.
# Quelle: docs/architecture/ws-adapter-design.md, Anhang B.
_FIELD_MAP: Final[dict[str, str]] = {
    "31": "last",
    "84": "bid",
    "86": "ask",
    "88": "bid_size",
    "85": "ask_size",
    "87": "volume",
    "7059": "last_size",
    "83": "change_pct",
    "6509": "availability_code",
    "70": "high",
    "71": "low",
    "6119": "server_id",
}

# Decimal-Felder (Preise) - IBKR sendet als String, oft mit ``"C"``-Praefix
# bei "ungeaendert seit Close". Wir entfernen das Praefix vor der Konversion.
_DECIMAL_FIELDS: Final[frozenset[str]] = frozenset(
    {"last", "bid", "ask", "high", "low"},
)

# Int-Felder (Sizes / Volume).
_INT_FIELDS: Final[frozenset[str]] = frozenset(
    {"bid_size", "ask_size", "volume", "last_size"},
)

# Float-Felder (relative Werte).
_FLOAT_FIELDS: Final[frozenset[str]] = frozenset({"change_pct"})

# String-Felder (Codes, Identifier).
_STRING_FIELDS: Final[frozenset[str]] = frozenset(
    {"availability_code", "server_id"},
)


CurrentSession = Literal["rth", "pre", "post", "closed", "halted"]
"""Re-exportierter Alias - damit Konsumenten denselben Literal-Typ wie
``broker_gateway.cp.tradeability`` verwenden koennen."""


@dataclass(frozen=True)
class SmdFrame:
    """Semantisch normierter Voll-Snapshot eines ``smd``-Frames.

    Felder ohne Wert (im aktuellen Snapshot noch nie gesehen) sind ``None``.
    Tradeability- und Exchange-Felder bleiben in K1 ``None`` und werden
    in K4 (``exchange_id``) bzw. K5 (``is_tradeable_now``,
    ``current_session``) gefuellt.
    """

    conid: int
    updated_at: str | int | None = None
    last: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    volume: int | None = None
    last_size: int | None = None
    change_pct: float | None = None
    availability_code: str | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    server_id: str | None = None
    exchange_id: str | None = None
    is_tradeable_now: bool | None = None
    current_session: CurrentSession | None = None


class SmdTopicAdapter:
    """Stateful Adapter, der ``smd``-Frames pro ``conid`` zu Voll-Snapshots
    aggregiert.

    Eine Adapter-Instanz pro Service-Lebenszeit. Der State ist nur in-memory;
    bei Service-Restart faellt der Snapshot-Cache weg und wird durch die
    naechsten Live-Frames neu aufgebaut.

    Tradeability-Anreicherung (AP-11 K5)
    ------------------------------------

    Optional kann der Adapter einen ``CalendarService`` und einen
    ``conid_to_exchange``-Lookup entgegennehmen; mit beiden setzt er pro
    Frame die Felder ``is_tradeable_now``, ``current_session`` und
    ``exchange_id``. Damit das im rein-synchronen ``feed()``-Pfad
    moeglich ist, muss der Aufrufer (typisch ``WSPushSource``) vor dem
    ersten Frame fuer einen ``conid`` einmal ``preload_for_conid(conid)``
    aufrufen - das macht den async REST-Call und befuellt einen lokalen
    Cache.

    Ohne CalendarService bleiben die drei Felder ``None``; das Verhalten
    ist 100 % rueckwaerts-kompatibel zu K1.
    """

    def __init__(
        self,
        *,
        calendar_service: CalendarService | None = None,
        conid_to_exchange: ConidToExchange | None = None,
        clock: ClockHook | None = None,
    ) -> None:
        self._snapshots: dict[int, dict[str, Any]] = {}
        self._last_updated: dict[int, Any] = {}
        self._calendar_service = calendar_service
        self._conid_to_exchange = conid_to_exchange
        self._clock = clock or _utcnow
        # Pro conid eine vorab aufgeloeste exchange_id. ``None`` markiert
        # einen bereits versuchten, aber fehlgeschlagenen Lookup, sodass
        # wir nicht bei jedem Frame neu fragen.
        self._conid_exchange: dict[int, str | None] = {}
        # Pro exchange_id der zuletzt vom CalendarService geholte
        # Schedule. Der CalendarService selbst hat einen 12h-TTL-Cache;
        # diese Map ist nur die synchron erreichbare Sicht im Adapter.
        self._exchange_calendars: dict[str, ExchangeCalendar] = {}

    def feed(self, raw_frame: dict[str, Any]) -> SmdFrame | None:
        """Verarbeitet einen rohen WS-Frame.

        Liefert einen ``SmdFrame``-Snapshot oder ``None``, wenn der Frame
        kein ``smd``-Frame ist, kein ``conid`` enthaelt oder ein Duplikat
        ist (gleiche ``(conid, _updated)``-Kombination wie vorher).
        """
        if not _is_smd_frame(raw_frame):
            return None

        conid = _extract_conid(raw_frame)
        if conid is None:
            return None

        updated = raw_frame.get("_updated")
        last_seen = self._last_updated.get(conid)
        if updated is not None and updated == last_seen:
            return None

        delta = _decode_fields(raw_frame)
        snapshot = self._snapshots.setdefault(conid, {})
        snapshot.update(delta)

        if updated is not None:
            self._last_updated[conid] = updated

        exchange_id = self._conid_exchange.get(conid)
        is_tradeable_now: bool | None = None
        current_session: CurrentSession | None = None
        if exchange_id is not None:
            calendar = self._exchange_calendars.get(exchange_id)
            if calendar is not None:
                code = snapshot.get("availability_code")
                tradeable, session = derive_tradeability(
                    now_utc=self._clock(),
                    calendar=calendar,
                    availability_code=code,
                )
                is_tradeable_now = tradeable
                current_session = session

        return SmdFrame(
            conid=conid,
            updated_at=updated,
            last=snapshot.get("last"),
            bid=snapshot.get("bid"),
            ask=snapshot.get("ask"),
            bid_size=snapshot.get("bid_size"),
            ask_size=snapshot.get("ask_size"),
            volume=snapshot.get("volume"),
            last_size=snapshot.get("last_size"),
            change_pct=snapshot.get("change_pct"),
            availability_code=snapshot.get("availability_code"),
            high=snapshot.get("high"),
            low=snapshot.get("low"),
            server_id=snapshot.get("server_id"),
            exchange_id=exchange_id,
            is_tradeable_now=is_tradeable_now,
            current_session=current_session,
        )

    async def preload_for_conid(self, conid: int) -> None:
        """Loest pro ``conid`` einmalig die ``exchange_id`` auf und holt
        den Schedule. Wird von ``WSPushSource.subscribe_quotes`` vor dem
        ersten Frame aufgerufen.

        Idempotent: doppelte Aufrufe nutzen den lokalen Cache und
        machen keinen weiteren REST-Call. Fehler werden geloggt, aber
        nicht propagiert - der Adapter laeuft im worst case einfach
        ohne Tradeability-Felder weiter.
        """
        if self._calendar_service is None or self._conid_to_exchange is None:
            return
        if conid in self._conid_exchange:
            return
        try:
            exchange_id = await self._conid_to_exchange(conid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SmdTopicAdapter: conid_to_exchange(%s) fehlgeschlagen: %s",
                conid,
                exc,
            )
            self._conid_exchange[conid] = None
            return
        if not exchange_id:
            self._conid_exchange[conid] = None
            return
        self._conid_exchange[conid] = exchange_id
        if exchange_id in self._exchange_calendars:
            return
        try:
            calendar = await self._calendar_service.get(exchange_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SmdTopicAdapter: CalendarService.get(%s) fehlgeschlagen: %s",
                exchange_id,
                exc,
            )
            return
        self._exchange_calendars[exchange_id] = calendar


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_smd_frame(raw_frame: dict[str, Any]) -> bool:
    topic = raw_frame.get("topic")
    return isinstance(topic, str) and topic.startswith("smd")


def _extract_conid(raw_frame: dict[str, Any]) -> int | None:
    conid = raw_frame.get("conid")
    if isinstance(conid, int):
        return conid
    if isinstance(conid, str) and conid.isdigit():
        return int(conid)
    # Fallback: aus dem Topic-Suffix ``smd+<conid>`` lesen.
    topic = raw_frame.get("topic")
    if isinstance(topic, str) and "+" in topic:
        suffix = topic.split("+", 1)[1]
        digits = suffix.split("_", 1)[0].split("@", 1)[0]
        if digits.isdigit():
            return int(digits)
    return None


def _decode_fields(raw_frame: dict[str, Any]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in raw_frame.items():
        if key not in _FIELD_MAP:
            continue
        name = _FIELD_MAP[key]
        coerced = _coerce(name, value)
        if coerced is not None:
            decoded[name] = coerced
    return decoded


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _DECIMAL_FIELDS:
        return _to_decimal(value)
    if name in _INT_FIELDS:
        return _to_int(value)
    if name in _FLOAT_FIELDS:
        return _to_float(value)
    if name in _STRING_FIELDS:
        return str(value)
    return value


def _to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        # IBKR praefixt "ungeaenderten" Last-Preis manchmal mit ``C``
        # (close), z.B. ``"C271.55"``. Andere Praefixe sind nicht bekannt -
        # wir strippen einen optionalen fuehrenden Buchstaben defensiv.
        text = value.strip()
        if text and text[0].isalpha():
            text = text[1:]
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError):
            return None
    return None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(Decimal(text))
        except (InvalidOperation, ValueError):
            return None
    return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
