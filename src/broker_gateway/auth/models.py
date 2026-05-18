"""Token- und Scope-Definitionen.

Single Source of Truth für Scope-Namen. Nirgends sonst im Code dürfen
Scope-Strings als Konstanten dupliziert werden - immer aus diesem Modul
importieren.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator


# Scope-Konstanten. Format `<bereich>:<recht>` mit `admin:*` als Wildcard.
SCOPE_QUOTES_READ = "quotes:read"
SCOPE_PORTFOLIO_READ = "portfolio:read"
SCOPE_INSTRUMENTS_READ = "instruments:read"
SCOPE_EVENTS_READ = "events:read"
SCOPE_ORDERS_READ = "orders:read"
SCOPE_ORDERS_WRITE = "orders:write"
SCOPE_HISTORICAL_READ = "historical:read"
SCOPE_FUNDAMENTALS_READ = "fundamentals:read"
SCOPE_ADMIN_ALL = "admin:*"

ALL_SCOPES: frozenset[str] = frozenset(
    {
        SCOPE_QUOTES_READ,
        SCOPE_PORTFOLIO_READ,
        SCOPE_INSTRUMENTS_READ,
        SCOPE_EVENTS_READ,
        SCOPE_ORDERS_READ,
        SCOPE_ORDERS_WRITE,
        SCOPE_HISTORICAL_READ,
        SCOPE_FUNDAMENTALS_READ,
        SCOPE_ADMIN_ALL,
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Token(BaseModel):
    """Opaques API-Token mit Scope-Claims.

    `value` ist die Bearer-Repräsentation und gleichzeitig der Primärschlüssel
    im Store. Niemals raten oder ableiten - der Wert wird beim Erstellen
    serverseitig generiert.
    """

    value: str = Field(min_length=16, description="Opaques Bearer-Token")
    scopes: list[str] = Field(default_factory=list)
    caller_id: str = Field(min_length=1, description="Logischer Consumer-Name (PSM, trading-robot, ...)")
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def _scopes_are_known(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - ALL_SCOPES)
        if unknown:
            raise ValueError(f"unbekannte Scopes: {unknown}")
        return v

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        ref = now if now is not None else _utcnow()
        return self.expires_at <= ref

    def has_scope(self, required: str) -> bool:
        if SCOPE_ADMIN_ALL in self.scopes:
            return True
        return required in self.scopes


class TokenCreate(BaseModel):
    """Request-Body für POST /v1/auth/token."""

    caller_id: str = Field(min_length=1)
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def _scopes_are_known(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - ALL_SCOPES)
        if unknown:
            raise ValueError(f"unbekannte Scopes: {unknown}")
        return v


class TokenView(BaseModel):
    """Public-Repräsentation eines Tokens.

    Wird in Responses zurückgegeben - enthält den `value` nur direkt nach
    dem Erzeugen (POST /v1/auth/token); spätere Listen-Views sollten den
    Wert ausblenden, sobald ein Listing-Endpunkt existiert.
    """

    value: str
    caller_id: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def from_token(cls, token: Token) -> "TokenView":
        return cls(
            value=token.value,
            caller_id=token.caller_id,
            scopes=list(token.scopes),
            created_at=token.created_at,
            expires_at=token.expires_at,
        )


def serialize_token(token: Token) -> dict[str, Any]:
    """JSON-serialisierbares Dict für FileTokenStore."""
    return {
        "value": token.value,
        "caller_id": token.caller_id,
        "scopes": list(token.scopes),
        "created_at": token.created_at.isoformat(),
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
    }


def deserialize_token(data: dict[str, Any]) -> Token:
    return Token(
        value=data["value"],
        caller_id=data["caller_id"],
        scopes=list(data.get("scopes", [])),
        created_at=datetime.fromisoformat(data["created_at"]),
        expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
    )
