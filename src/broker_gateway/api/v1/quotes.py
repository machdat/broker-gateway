"""GET /v1/quotes/snapshot - Marktdaten-Snapshot mit First-Call-Prime."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_QUOTES_READ, Token
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok
from broker_gateway.cp.quotes import (
    Quote,
    QuotesService,
    normalize_default_fields,
    resolve_fields,
)


router = APIRouter(prefix="/quotes", tags=["quotes"])

# CP-Gateway-Limit (Anhang Abschnitt 4): max. 5 conids in einem Snapshot-Call.
MAX_SNAPSHOT_CONIDS = 5


def get_quotes_service() -> QuotesService:
    raise RuntimeError(
        "get_quotes_service muss in der App per dependency_overrides gesetzt werden"
    )


def _parse_conids(raw: str) -> list[int]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="conids darf nicht leer sein",
        )
    if len(parts) > MAX_SNAPSHOT_CONIDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Maximal {MAX_SNAPSHOT_CONIDS} conids pro Snapshot-Request",
        )
    try:
        return [int(p) for p in parts]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"conids muss Komma-separierte Integer enthalten: {exc}",
        ) from exc


@router.get(
    "/snapshot",
    response_model=list[Quote],
    summary="Snapshot fuer bis zu 5 conids (First-Call-Prime intern absorbiert)",
)
async def quotes_snapshot(
    conids: Annotated[str, Query(min_length=1, description="Komma-separierte conid-Liste, max 5")],
    _scope: Annotated[Token, Depends(require_scope(SCOPE_QUOTES_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[QuotesService, Depends(get_quotes_service)],
    fields: Annotated[
        str | None,
        Query(description="Komma-separierte Feldnamen (last, bid, ask, volume, change_pct, high, low, availability)"),
    ] = None,
) -> list[Quote]:
    parsed_conids = _parse_conids(conids)
    field_names = (
        [f.strip() for f in fields.split(",") if f.strip()]
        if fields
        else list(normalize_default_fields())
    )
    resolved = resolve_fields(field_names)
    return await service.snapshot_with_prime(parsed_conids, resolved)
