from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, cast

from fastapi import FastAPI

from broker_gateway import __version__
from broker_gateway.api.v1 import router as v1_router
from broker_gateway.api.v1.instruments import get_instruments_service
from broker_gateway.auth.middleware import get_token_store
from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.auth.store import TokenStore, build_default_store
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import InstrumentsService
from broker_gateway.cp.lifecycle import AuthLifecycle, get_cp_lifecycle


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


def create_app(
    *,
    store: TokenStore | None = None,
    lifecycle: AuthLifecycle | None = None,
    instruments_service: InstrumentsService | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_lifecycle = lifecycle is None
        if owns_lifecycle:
            client = CPGatewayClient()
            cp_lifecycle = AuthLifecycle(client)
        else:
            client = None
            cp_lifecycle = lifecycle

        app.state.cp_lifecycle = cp_lifecycle
        app.dependency_overrides[get_cp_lifecycle] = lambda: cast(AuthLifecycle, app.state.cp_lifecycle)

        owns_instruments = instruments_service is None
        if owns_instruments:
            instruments_client = client if client is not None else CPGatewayClient()
            inst_service = InstrumentsService(instruments_client)
            app.state._owns_instruments_client = client is None
            app.state._instruments_client = instruments_client
        else:
            inst_service = instruments_service
            app.state._owns_instruments_client = False
            app.state._instruments_client = None

        app.state.instruments_service = inst_service
        app.dependency_overrides[get_instruments_service] = (
            lambda: cast(InstrumentsService, app.state.instruments_service)
        )

        await cp_lifecycle.start()
        try:
            yield
        finally:
            await cp_lifecycle.stop()
            if client is not None:
                await client.aclose()
            if app.state._owns_instruments_client and app.state._instruments_client is not None:
                await app.state._instruments_client.aclose()

    app = FastAPI(
        title="broker-gateway",
        version=__version__,
        description="Versionierte HTTP-API fuer broker-vermittelten Aktienhandel und Marktdaten-Streaming.",
        lifespan=lifespan,
    )
    app.include_router(v1_router)

    actual_store = store if store is not None else build_default_store()
    _ensure_bootstrap_admin(actual_store)
    app.state.token_store = actual_store
    app.dependency_overrides[get_token_store] = lambda: cast(TokenStore, app.state.token_store)
    return app


app = create_app()
