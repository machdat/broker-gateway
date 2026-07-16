"""Topic-Adapter fuer IBKR ``sor`` (Live Order Updates) Frames.

Mappt rohe ``sor``-Frames vom WS-Client auf die semantischen Order-
Felder aus K6-Anhang A. Verhalten:

- **Voll-Snapshot pro order_id.** IBKR liefert pro Order zuerst einen
  vollstaendigen Snapshot, danach Status-Deltas. Der Adapter haelt pro
  ``order_id`` den letzten Snapshot und merged Delta-Felder hinein.
- **Status-Normalisierung.** IBKR-Werte (``Inactive``,
  ``PendingSubmit``, ``PreSubmitted``, ``Submitted``, ``Filled``,
  ``Cancelled``, ``PendingCancel``, ``Rejected``) werden auf ein
  semantisches Set ``pending`` / ``accepted`` / ``partial_fill`` /
  ``filled`` / ``cancelled`` / ``rejected`` reduziert.
- **timeInForce-Quirk.** ``CLOSE`` (IBKR-internes Tagging fuer
  DAY-Order ausserhalb RTH) wird auf ``DAY`` gemappt.
- **UI-Filter.** ``bgColor`` / ``fgColor`` aus den TWS-internen
  Status-Frames werden NICHT durchgereicht.

Bewusst NICHT in K6: Slow-Consumer-Drop, Backpressure-Heuristik. Das
bleibt eine Folge-Karte (K8/Querschnitt).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal


OrderStatus = Literal[
    "pending",
    "accepted",
    "partial_fill",
    "filled",
    "cancelled",
    "rejected",
]


# IBKR-Status -> semantischer Status. Nicht gelistete Werte fallen auf
# ``pending`` zurueck (defensiver Default - bei unbekannten Status-
# Strings will der Konsument nicht aus Versehen ``filled`` sehen).
_STATUS_MAP: Final[dict[str, OrderStatus]] = {
    "INACTIVE": "pending",
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
    "REJECTED": "rejected",
}

# IBKR-Wire-Schluessel -> Adapter-Feldname (semantisch). Quelle:
# K6-Anhang A.
_FIELD_MAP: Final[dict[str, str]] = {
    "orderId": "order_id",
    "order_id": "order_id",
    "cOID": "client_order_id",
    "parentId": "parent_id",
    "acct": "account",
    "ticker": "symbol",
    "side": "side",
    "totalSize": "quantity",
    "filledQuantity": "filled_quantity",
    "avgPrice": "avg_fill_price",
    "status": "status",
    "timeInForce": "time_in_force",
    "lastExecutionTime": "last_event_at",
    "orderRejectReason": "reject_reason",
    "conid": "conid",
}

_DECIMAL_FIELDS: Final[frozenset[str]] = frozenset(
    {"quantity", "filled_quantity", "avg_fill_price"}
)
_INT_FIELDS: Final[frozenset[str]] = frozenset({"order_id", "parent_id", "conid"})


@dataclass(frozen=True)
class SorFrame:
    """Voll-Snapshot eines Order-Updates."""

    order_id: int
    account: str | None = None
    client_order_id: str | None = None
    parent_id: int | None = None
    symbol: str | None = None
    side: str | None = None
    quantity: Decimal | None = None
    filled_quantity: Decimal | None = None
    avg_fill_price: Decimal | None = None
    status: OrderStatus | None = None
    # Roher IBKR-Status-String (z.B. "PendingCancel", "Inactive"), additiv zum
    # semantisch reduzierten ``status``. So bleibt PENDINGCANCEL/INACTIVE (beide
    # fallen im ``status`` auf ``pending``) von einem echten ``pending``
    # unterscheidbar (Karte a04adca8). Nur der TWS-Pfad fuellt ihn; der
    # cp-Adapter normalisiert den Status frueh und laesst ihn None.
    raw_status: str | None = None
    time_in_force: str | None = None
    last_event_at: str | None = None
    reject_reason: str | None = None
    conid: int | None = None


class SorTopicAdapter:
    """Stateful Adapter fuer ``sor``-Frames.

    Pro ``order_id`` wird ein Snapshot gehalten; Folgeframes mergen
    Delta-Felder. UI-Felder (``bgColor`` / ``fgColor``) werden gefiltert,
    ``status`` und ``timeInForce`` werden normalisiert.
    """

    def __init__(self) -> None:
        self._snapshots: dict[int, dict[str, Any]] = {}

    def feed(self, raw_frame: dict[str, Any]) -> SorFrame | None:
        """Verarbeitet einen rohen sor-Frame.

        Liefert einen Voll-Snapshot oder ``None``, wenn der Frame kein
        sor-Frame ist oder keine ``order_id`` enthaelt.
        """
        if not _is_sor_frame(raw_frame):
            return None
        delta = _decode_fields(raw_frame)
        order_id = delta.get("order_id")
        if not isinstance(order_id, int):
            return None
        snapshot = self._snapshots.setdefault(order_id, {})
        snapshot.update(delta)
        return SorFrame(
            order_id=order_id,
            account=snapshot.get("account"),
            client_order_id=snapshot.get("client_order_id"),
            parent_id=snapshot.get("parent_id"),
            symbol=snapshot.get("symbol"),
            side=snapshot.get("side"),
            quantity=snapshot.get("quantity"),
            filled_quantity=snapshot.get("filled_quantity"),
            avg_fill_price=snapshot.get("avg_fill_price"),
            status=snapshot.get("status"),
            time_in_force=snapshot.get("time_in_force"),
            last_event_at=snapshot.get("last_event_at"),
            reject_reason=snapshot.get("reject_reason"),
            conid=snapshot.get("conid"),
        )

    def bootstrap(self, orders: list[dict[str, Any]]) -> list[SorFrame]:
        """Speist eine Liste REST-Bootstrap-Orders in den Snapshot-Cache.

        Nutzt denselben Decode-Pfad wie ``feed`` und liefert pro Order
        einen Voll-Snapshot zurueck. Frames ohne ``order_id`` werden
        ignoriert.
        """
        result: list[SorFrame] = []
        for raw in orders:
            if not isinstance(raw, dict):
                continue
            # REST-Bootstrap-Eintraege haben kein ``topic``-Feld; wir
            # fuegen es defensiv ein, damit ``_is_sor_frame`` greift.
            patched = {**raw, "topic": raw.get("topic", "sor")}
            frame = self.feed(patched)
            if frame is not None:
                result.append(frame)
        return result


def _is_sor_frame(raw_frame: dict[str, Any]) -> bool:
    topic = raw_frame.get("topic")
    if isinstance(topic, str) and topic.startswith("sor"):
        return True
    # Manche IBKR-Antworten markieren den Frame nicht; wir akzeptieren,
    # wenn ``orderId`` vorhanden ist (typisch fuer REST-Bootstrap).
    return "orderId" in raw_frame or "order_id" in raw_frame


def _decode_fields(raw_frame: dict[str, Any]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in raw_frame.items():
        # UI-only-Felder werden nicht durchgereicht.
        if key in {"bgColor", "fgColor"}:
            continue
        target = _FIELD_MAP.get(key)
        if target is None:
            continue
        coerced = _coerce(target, value)
        if coerced is not None:
            decoded[target] = coerced
    return decoded


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name == "status":
        return _normalise_status(value)
    if name == "time_in_force":
        return _normalise_time_in_force(value)
    if name in _DECIMAL_FIELDS:
        return _to_decimal(value)
    if name in _INT_FIELDS:
        return _to_int(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal, bool)):
        return value
    return value


def _normalise_status(value: Any) -> OrderStatus:
    text = str(value).strip().upper().replace(" ", "")
    return _STATUS_MAP.get(text, "pending")


def _normalise_time_in_force(value: Any) -> str:
    text = str(value).strip().upper()
    if text == "CLOSE":
        return "DAY"
    return text


def _to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
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
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    if isinstance(value, float):
        return int(value)
    return None


__all__ = ["OrderStatus", "SorFrame", "SorTopicAdapter"]
