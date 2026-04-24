from __future__ import annotations

import os
from typing import cast

from fastapi import FastAPI

from broker_gateway import __version__
from broker_gateway.api.v1 import router as v1_router
from broker_gateway.auth.middleware import get_token_store
from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.auth.store import TokenStore, build_default_store


_BOOTSTRAP_CALLER_ID = "bootstrap-admin"


def _ensure_bootstrap_admin(store: TokenStore) -> None:
    bootstrap = os.environ.get("BG_BOOTSTRAP_ADMIN_TOKEN")
    if not bootstrap:
        return
    existing = store.get(bootstrap)
    if existing is not None:
        return
    store.put(
        Token(
            value=bootstrap,
            caller_id=_BOOTSTRAP_CALLER_ID,
            scopes=[SCOPE_ADMIN_ALL],
        )
    )


def create_app(*, store: TokenStore | None = None) -> FastAPI:
    app = FastAPI(
        title="broker-gateway",
        version=__version__,
        description="Versionierte HTTP-API fuer broker-vermittelten Aktienhandel und Marktdaten-Streaming.",
    )
    app.include_router(v1_router)

    actual_store = store if store is not None else build_default_store()
    _ensure_bootstrap_admin(actual_store)
    app.state.token_store = actual_store
    app.dependency_overrides[get_token_store] = lambda: cast(TokenStore, app.state.token_store)
    return app


app = create_app()
