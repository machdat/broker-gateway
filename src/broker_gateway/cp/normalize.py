"""Normalisiert nicht-deterministische Felder in CP-Gateway-Responses.

Single Source of Truth fuer Recorder + spaeteren Replay-Loader. Ohne
diese Schicht waeren aufgezeichnete Fixtures bei jedem Live-Run anders
(Timestamps, Order-IDs), weil IBKR diese Felder pro Session neu
vergibt. Preise und Marktdaten bleiben absichtlich unberuehrt - das ist
die Realitaet, gegen die Tests prueften sollen.
"""
from __future__ import annotations

import re
from typing import Any


# ISO-8601: 2026-04-25T10:30:00, optional ms/timezone, "T" oder " " als Trennzeichen.
_ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)

_TIMESTAMP_FIELDS_LOWER: frozenset[str] = frozenset({
    "recorded_at", "trade_time", "ssoexpires", "_updated",
    "lastupdated", "last_updated", "expiry", "starttime", "endtime",
})

_ID_FIELDS_LOWER: dict[str, str] = {
    "order_id": "ORDER_ID",
    "orderid": "ORDER_ID",
    "execution_id": "EXEC_ID",
    "executionid": "EXEC_ID",
    "exec_id": "EXEC_ID",
    "execid": "EXEC_ID",
    "reply_id": "REPLY_ID",
    "replyid": "REPLY_ID",
}

# Felder, deren Inhalt eine sessionId-aehnliche Caller-Identifikation ist.
_SESSION_FIELDS_LOWER: frozenset[str] = frozenset({
    "session", "sessionid", "session_id",
})

# sso/validate liefert auth-Tokens, User-IDs, Credentials und externe IP -
# alles strikt sensibel. AP-02 #07-4 hat den Leak im ersten Live-Recording
# entdeckt. Werte werden auf <REDACTED> gesetzt; das Feld bleibt im
# Body, damit Replay-Loader/Drift-Check Schema-Aenderungen erkennen.
_SECRET_FIELDS_LOWER: frozenset[str] = frozenset({
    "token", "credential", "credentials", "user_name", "username",
    "user_id", "userid", "unique_login_id", "uniqueloginid",
    "ip", "ip_address", "ipaddress", "hardware_info", "hardwareinfo",
    "auth_time", "authtime", "expires", "took",
    "sft", "sf_config", "credential_type",
    "mac",  # MAC-Adresse aus iserver/auth/status
})

# Preis- und Marktdaten-Felder bleiben default unberuehrt. Endpunkte, die
# das umstellen wollen, setzen normalize_prices=True.
_PRICE_FIELDS_LOWER: frozenset[str] = frozenset({
    "price", "avg_price", "avgprice", "avgcost", "avg_cost",
    "limit_price", "limitprice", "stop_price", "stopprice",
    "mktprice", "mkt_price", "mktvalue", "mkt_value",
    "settledcash", "cashbalance", "net_amount", "netamount",
    "filled_quantity", "size", "quantity", "qty", "position",
})

# IBKR-Snapshot-Field-IDs, die Marktdaten enthalten (31=last, 84=bid, 86=ask).
_MARKETDATA_NUMERIC_FIELDS: frozenset[str] = frozenset({
    "31", "84", "86", "85", "87", "88",
})


class _NormalizeState:
    """Pro normalize_response()-Aufruf: Counter fuer ID-Platzhalter.

    Gleicher Roh-Wert in derselben Antwort bekommt denselben Platzhalter -
    so bleiben interne Referenzen (z.B. Reply-ID -> spaetere Bestaetigung)
    konsistent.
    """

    __slots__ = ("normalize_prices", "_buckets")

    def __init__(self, *, normalize_prices: bool) -> None:
        self.normalize_prices = normalize_prices
        self._buckets: dict[str, dict[str, str]] = {}

    def placeholder(self, kind: str, raw_value: str) -> str:
        bucket = self._buckets.setdefault(kind, {})
        if raw_value not in bucket:
            bucket[raw_value] = f"<{kind}_{len(bucket) + 1:03d}>"
        return bucket[raw_value]


def normalize_response(
    payload: Any,
    endpoint: str,
    *,
    normalize_prices: bool = False,
) -> Any:
    """Liefert eine normalisierte Kopie von ``payload``.

    Args:
        payload: Geparste JSON-Struktur (dict/list/skalar).
        endpoint: Pfad des CP-Gateway-Calls (z.B. "/iserver/account/orders").
            Aktuell nicht endpunkt-spezifisch ausgewertet, aber im Signature
            verankert, damit Folge-Karten endpunkt-Listen einbauen koennen,
            ohne die Aufrufer zu touchieren.
        normalize_prices: Wenn True, werden auch Preis-Felder durch
            "<PRICE>" ersetzt. Default False - Preise sind Realitaet, gegen
            die Tests laufen sollen.
    """
    state = _NormalizeState(normalize_prices=normalize_prices)
    _ = endpoint  # noqa: F841 - Hook fuer endpunkt-spezifische Folgeregeln
    return _walk(payload, state, parent_key=None)


def _walk(value: Any, state: _NormalizeState, *, parent_key: str | None) -> Any:
    if isinstance(value, dict):
        return {k: _walk(v, state, parent_key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(item, state, parent_key=parent_key) for item in value]
    return _scalar(value, state, parent_key=parent_key)


def _scalar(value: Any, state: _NormalizeState, *, parent_key: str | None) -> Any:
    if parent_key is None:
        return value

    key_lower = parent_key.lower()

    if key_lower in _SESSION_FIELDS_LOWER and value not in (None, ""):
        return "<SESSION_ID>"

    if (
        key_lower in _SECRET_FIELDS_LOWER
        and value not in (None, "")
        and not isinstance(value, bool)
    ):
        return "<REDACTED>"

    if (kind := _ID_FIELDS_LOWER.get(key_lower)) and value not in (None, ""):
        return state.placeholder(kind, str(value))

    if _is_timestamp_field(parent_key) and value is not None:
        return "<TIMESTAMP>"

    if isinstance(value, str) and _ISO_TS_RE.match(value):
        return "<TIMESTAMP>"

    if state.normalize_prices and (
        key_lower in _PRICE_FIELDS_LOWER or parent_key in _MARKETDATA_NUMERIC_FIELDS
    ):
        return "<PRICE>"

    return value


def _is_timestamp_field(field_name: str) -> bool:
    lower = field_name.lower()
    if lower in _TIMESTAMP_FIELDS_LOWER:
        return True
    return lower.endswith(("_at", "_ts"))
