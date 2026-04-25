"""FastAPI-Dependencies für Auth.

Aufrufer hängen die Dependency in den jeweiligen Endpunkt:

    @router.get("/portfolio", dependencies=[Depends(require_scope(SCOPE_PORTFOLIO_READ))])

Der Store wird über `get_token_store` injected und in `main.py` per
`app.dependency_overrides[get_token_store] = ...` ausgetauscht (z.B.
in Tests).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from broker_gateway.auth.models import Token
from broker_gateway.auth.store import TokenStore


# Wird in main.py per app.dependency_overrides[...] auf den App-Singleton
# gemappt. Direkter Aufruf liefert nichts Sinnvolles - daher RuntimeError.
def get_token_store() -> TokenStore:
    raise RuntimeError(
        "get_token_store muss in der App per dependency_overrides gesetzt werden"
    )


def _parse_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization-Header fehlt",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization-Header muss 'Bearer <token>' sein",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1].strip()


def get_current_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    store: TokenStore = Depends(get_token_store),
) -> Token:
    raw = _parse_bearer(authorization)
    token = store.get(raw)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token unbekannt oder revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if token.is_expired():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token abgelaufen",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.auth_token = token
    return token


def require_scope(required: str):
    """Dependency-Factory: liefert eine FastAPI-Dependency, die den Token
    holt und gegen `required` prüft. `admin:*` matcht alles.
    """

    def _dep(token: Annotated[Token, Depends(get_current_token)]) -> Token:
        if not token.has_scope(required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Token besitzt nicht den erforderlichen Scope '{required}'",
            )
        return token

    return _dep
