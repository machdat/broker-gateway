from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, cast

from fastapi import FastAPI

from broker_gateway import __version__
from broker_gateway.api.v1 import router as v1_router
from broker_gateway.api.v1.instruments import get_instruments_service
from broker_gateway.api.v1.quotes import get_quotes_service
from broker_gateway.auth.middleware import get_token_store
from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.auth.store import TokenStore, build_default_store
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import InstrumentsService
from broker_gateway.cp.lifecycle import AuthLifecycle, get_cp_lifecycle
from broker_gateway.cp.quotes import QuotesService
from broker_gateway.streams.manager import (
    SubscriptionManager,
    get_subscription_manager,
)


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
    quotes_service: QuotesService | None = None,
    subscription_manager: SubscriptionManager | None = None,
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

        services_client: CPGatewayClient | None = client
        services_owned = False
        if (
            instruments_service is None
            or quotes_service is None
            or subscription_manager is None
        ):
            if services_client is None:
                services_client = CPGatewayClient()
                services_owned = True

        app.state._owns_services_client = services_owned
        app.state._services_client = services_client

        inst_service = (
            instruments_service
            if instruments_service is not None
            else InstrumentsService(cast(CPGatewayClient, services_client))
        )
        qts_service = (
            quotes_service
            if quotes_service is not None
            else QuotesService(cast(CPGatewayClient, services_client))
        )
        sub_manager = (
            subscription_manager
            if subscription_manager is not None
            else SubscriptionManager(cast(CPGatewayClient, services_client))
        )

        app.state.instruments_service = inst_service
        app.state.quotes_service = qts_service
        app.state.subscription_manager = sub_manager
        app.dependency_overrides[get_instruments_service] = (
            lambda: cast(InstrumentsService, app.state.instruments_service)
        )
        app.dependency_overrides[get_quotes_service] = (
            lambda: cast(QuotesService, app.state.quotes_service)
        )
        app.dependency_overrides[get_subscription_manager] = (
            lambda: cast(SubscriptionManager, app.state.subscription_manager)
        )

        await cp_lifecycle.start()
        try:
            yield
        finally:
            await sub_manager.shutdown()
            await cp_lifecycle.stop()
            if client is not None:
                await client.aclose()
            if app.state._owns_services_client and app.state._services_client is not None:
                await app.state._services_client.aclose()

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
