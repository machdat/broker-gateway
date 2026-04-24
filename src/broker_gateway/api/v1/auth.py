"""Auth-Endpunkte für /v1/auth/token (Erstellen, Revoke)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from broker_gateway.auth.middleware import (
    get_current_token,
    get_token_store,
    require_scope,
)
from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    Token,
    TokenCreate,
    TokenView,
)
from broker_gateway.auth.store import TokenStore, generate_token_value


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/token",
    response_model=TokenView,
    status_code=status.HTTP_201_CREATED,
    summary="Erzeugt ein neues API-Token",
)
def create_token(
    payload: TokenCreate,
    _admin: Annotated[Token, Depends(require_scope(SCOPE_ADMIN_ALL))],
    store: Annotated[TokenStore, Depends(get_token_store)],
) -> TokenView:
    token = Token(
        value=generate_token_value(),
        caller_id=payload.caller_id,
        scopes=list(payload.scopes),
        expires_at=payload.expires_at,
    )
    store.put(token)
    return TokenView.from_token(token)


@router.delete(
    "/token",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke - admin:* darf alle, sonst nur Self",
)
def revoke_token(
    current: Annotated[Token, Depends(get_current_token)],
    store: Annotated[TokenStore, Depends(get_token_store)],
    value: Annotated[str | None, Query(description="Zu revokendes Token. Ohne Wert: das Bearer-Token aus dem Request.")] = None,
) -> None:
    target_value = value if value is not None else current.value
    if target_value != current.value and not current.has_scope(SCOPE_ADMIN_ALL):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Fremde Tokens revoken erfordert admin:*",
        )
    if not store.delete(target_value):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token nicht gefunden",
        )
    return None
