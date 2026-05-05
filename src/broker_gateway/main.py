from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from broker_gateway import __version__
from broker_gateway.api.metrics import router as metrics_router
from broker_gateway.api.v1 import router as v1_router
from broker_gateway.api.v1.errors import (
    CPGatewayError,
    build_error_response,
    default_code_for,
)
from broker_gateway.api.v1.exchanges import get_calendar_service
from broker_gateway.api.v1.instruments import get_instruments_service
from broker_gateway.api.v1.orders_stream import (
    OrdersBootstrapLoader,
    get_orders_bootstrap_loader,
)
from broker_gateway.api.v1.status import StatusProbe, get_status_probe
from broker_gateway.api.v1.orders import (
    get_idempotency_store,
    get_orders_portfolio_invalidator,
    get_orders_service,
)
from broker_gateway.api.v1.portfolio import get_portfolio_service
from broker_gateway.api.v1.quotes import get_quotes_service
from broker_gateway.api.v1.trades import get_trades_service
from broker_gateway.auth.middleware import get_token_store
from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.auth.store import TokenStore, build_default_store
from broker_gateway.config import quotes_source as _read_quotes_source
from broker_gateway.config import validate_runtime_config
from broker_gateway.cp.calendar import CalendarService
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import InstrumentsService
from broker_gateway.cp.lifecycle import AuthLifecycle, AuthStatus, get_cp_lifecycle
from broker_gateway.cp.orders import OrdersService
from broker_gateway.cp.portfolio import PortfolioService
from broker_gateway.cp.quotes import QuotesService
from broker_gateway.cp.topics.smd import SmdTopicAdapter
from broker_gateway.cp.topics.sor import SorTopicAdapter
from broker_gateway.cp.trades import TradesService
from broker_gateway.cp.ws_client import CPWebSocketClient
from broker_gateway.idempotency import IdempotencyStore
from broker_gateway.logging_setup import configure_logging
from broker_gateway.metrics import (
    BrokerGatewayMetrics,
    get_metrics,
    make_live_collector,
)
from broker_gateway.middleware.observability import ObservabilityMiddleware
from broker_gateway.streams.events import EventBus, get_event_bus
from broker_gateway.streams.manager import (
    SubscriptionManager,
    get_subscription_manager,
)
from broker_gateway.streams.orders import (
    OrdersBroadcaster,
    get_orders_broadcaster,
)
from broker_gateway.streams.registry import SubscriptionRegistry
from broker_gateway.streams.ws_source import WSPushSource
from broker_gateway.throttle.manager import ThrottleManager, get_throttle_manager


_BOOTSTRAP_CALLER_ID = "bootstrap-admin"

_logger = logging.getLogger(__name__)


def _format_cookie_header(client: CPGatewayClient | None) -> str:
    """Liest httpx-Cookies aus dem REST-Client und liefert sie als
    ``name=value; ...``-Header-Wert.

    Wird vom WS-Lifespan-Init genutzt, um den Browser-Cookie-Sandbox-
    Workflow nachzubauen, den IBKR fuer den /v1/api/ws-Handshake
    erwartet. Defensive: leerer String wenn Jar leer oder Client None.
    """
    if client is None:
        return ""
    httpx_client = getattr(client, "_client", None)
    cookies = getattr(httpx_client, "cookies", None)
    if cookies is None:
        return ""
    parts: list[str] = []
    try:
        for cookie in cookies.jar:
            parts.append(f"{cookie.name}={cookie.value}")
    except Exception:  # noqa: BLE001
        return ""
    return "; ".join(parts)


async def _build_subscription_layer(
    *,
    services_client: CPGatewayClient,
    cp_lifecycle: AuthLifecycle,
    inst_service: InstrumentsService,
    cal_service: CalendarService,
    override_manager: SubscriptionManager | None,
) -> tuple[SubscriptionManager, StatusProbe, CPWebSocketClient | None, WSPushSource | None]:
    """Baut SubscriptionManager + StatusProbe nach dem Lifecycle-Start.

    Bei ``BG_QUOTES_SOURCE=ws`` und gueltiger Session wird ein
    ``CPWebSocketClient`` geoeffnet und ueber einen ``WSPushSource`` an
    den Manager verdrahtet. Sonst (oder bei Connect-Fehler) wird der
    klassische Polling-Manager gebaut. ``override_manager`` hat Vorrang
    und ueberspringt jede WS-Heuristik (Test-Pfad).
    """
    if override_manager is not None:
        return override_manager, StatusProbe(), None, None

    requested = _read_quotes_source()
    if requested != "ws":
        return SubscriptionManager(services_client), StatusProbe(), None, None

    snapshot = cp_lifecycle.snapshot()
    if snapshot.auth_status is not AuthStatus.OK or not snapshot.session_id:
        _logger.warning(
            "BG_QUOTES_SOURCE=ws angefordert, aber Session nicht bereit "
            "(auth_status=%s, session_id_present=%s) - fallback polling",
            snapshot.auth_status.value,
            snapshot.session_id is not None,
        )
        return SubscriptionManager(services_client), StatusProbe(), None, None

    async def _conid_to_exchange(conid: int) -> str | None:
        try:
            detail = await inst_service.info(conid)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("conid_to_exchange(%s) fehlgeschlagen: %s", conid, exc)
            return None
        return detail.exchange_id

    smd_adapter = SmdTopicAdapter(
        calendar_service=cal_service,
        conid_to_exchange=_conid_to_exchange,
    )
    ws_client = CPWebSocketClient()

    async def _registry_subscribe(topic: str, args: dict[str, Any]) -> None:
        """Subscribe-Callback fuer SubscriptionRegistry-Replay nach
        Reconnect. Baut den IBKR-spezifischen smd-Subscribe-Frame und
        schickt ihn ueber den WS-Client."""
        if topic != "smd":
            return
        conid = args.get("conid")
        fields = args.get("fields") or ()
        body = json.dumps({"fields": list(fields)}, separators=(",", ":"))
        try:
            await ws_client.send(f"smd+{conid}+{body}")
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Registry-Replay-Subscribe fuer conid=%s fehlgeschlagen: %s",
                conid,
                exc,
            )

    registry = SubscriptionRegistry(_registry_subscribe)
    cookies = _format_cookie_header(services_client)
    try:
        await ws_client.connect(snapshot.session_id, cookies)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "CPWebSocketClient.connect fehlgeschlagen, fallback polling: %s", exc
        )
        try:
            await ws_client.aclose()
        except Exception:  # noqa: BLE001
            pass
        return SubscriptionManager(services_client), StatusProbe(), None, None

    holder: list[WSPushSource] = []

    async def _ws_subscribe(conid: int, fields: set[str]) -> None:
        if holder:
            await holder[0].subscribe_quotes(conid, fields)

    async def _ws_unsubscribe(conid: int) -> None:
        if holder:
            await holder[0].unsubscribe_quotes(conid)

    sub_manager = SubscriptionManager(
        services_client,
        quotes_source="ws",
        ws_subscribe=_ws_subscribe,
        ws_unsubscribe=_ws_unsubscribe,
    )
    ws_source = WSPushSource(ws_client, smd_adapter, registry, sub_manager)
    holder.append(ws_source)
    await ws_source.start()

    status_probe = StatusProbe(
        registry=registry,
        ws_reconnect_attempt=lambda: ws_client.reconnect_attempt,
    )
    _logger.info(
        "WS-Lifespan aktiv: BG_QUOTES_SOURCE=ws, session_id_present=true"
    )
    return sub_manager, status_probe, ws_client, ws_source


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
    calendar_service: CalendarService | None = None,
    portfolio_service: PortfolioService | None = None,
    orders_service: OrdersService | None = None,
    idempotency_store: IdempotencyStore | None = None,
    trades_service: TradesService | None = None,
    event_bus: EventBus | None = None,
    throttle: ThrottleManager | None = None,
    metrics: BrokerGatewayMetrics | None = None,
) -> FastAPI:
    configure_logging()
    actual_metrics = metrics if metrics is not None else BrokerGatewayMetrics()
    actual_throttle_outer = throttle if throttle is not None else ThrottleManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Hard-Guard 1: BG_STACK_KIND-Pflicht, Live-vs-Paper-Mismatch
        # und BG_PAPER_AUTO_LOGIN-Konsistenz. ConfigError bricht den
        # Lifespan ab, bevor wir Services hochziehen oder das CP-
        # Gateway anfassen. Bewusst im Lifespan und nicht in create_app
        # selbst, damit Modul-Level ``app = create_app()`` ohne Env
        # importierbar bleibt — die echte Pruefung passiert erst beim
        # ersten Lifespan-Run (uvicorn / TestClient).
        validate_runtime_config()
        actual_throttle = actual_throttle_outer
        owns_lifecycle = lifecycle is None
        if owns_lifecycle:
            client = CPGatewayClient(throttle=actual_throttle)
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
            or portfolio_service is None
            or orders_service is None
            or trades_service is None
        ):
            if services_client is None:
                services_client = CPGatewayClient(throttle=actual_throttle)
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
        cal_service = (
            calendar_service
            if calendar_service is not None
            else CalendarService(cast(CPGatewayClient, services_client))
        )
        orders_broadcaster = OrdersBroadcaster()
        sor_adapter = SorTopicAdapter()
        orders_bootstrap = OrdersBootstrapLoader(
            cast(CPGatewayClient, services_client), sor_adapter
        )
        # SubscriptionManager + StatusProbe werden nach cp_lifecycle.start()
        # gebaut, damit der WS-Pfad (BG_QUOTES_SOURCE=ws, AP-11 K9) eine
        # gueltige Session-ID + Cookies vom AuthLifecycle uebernehmen
        # kann. Bei Polling oder nicht-OK Session faellt der Block auf
        # den klassischen Polling-Pfad zurueck.
        sub_manager: SubscriptionManager | None = None
        status_probe: StatusProbe | None = None
        ws_client: CPWebSocketClient | None = None
        ws_source: WSPushSource | None = None
        pf_service = (
            portfolio_service
            if portfolio_service is not None
            else PortfolioService(cast(CPGatewayClient, services_client))
        )
        ord_service = (
            orders_service
            if orders_service is not None
            else OrdersService(cast(CPGatewayClient, services_client))
        )
        idem_store = (
            idempotency_store if idempotency_store is not None else IdempotencyStore()
        )
        trd_service = (
            trades_service
            if trades_service is not None
            else TradesService(cast(CPGatewayClient, services_client))
        )
        evt_bus = event_bus if event_bus is not None else EventBus()

        app.state.instruments_service = inst_service
        app.state.quotes_service = qts_service
        app.state.calendar_service = cal_service
        app.state.orders_broadcaster = orders_broadcaster
        app.state.orders_bootstrap_loader = orders_bootstrap
        app.state.portfolio_service = pf_service
        app.state.orders_service = ord_service
        app.state.idempotency_store = idem_store
        app.state.trades_service = trd_service
        app.state.event_bus = evt_bus
        app.state.throttle_manager = actual_throttle
        app.state.metrics = actual_metrics
        app.dependency_overrides[get_throttle_manager] = (
            lambda: cast(ThrottleManager, app.state.throttle_manager)
        )
        app.dependency_overrides[get_instruments_service] = (
            lambda: cast(InstrumentsService, app.state.instruments_service)
        )
        app.dependency_overrides[get_quotes_service] = (
            lambda: cast(QuotesService, app.state.quotes_service)
        )
        app.dependency_overrides[get_calendar_service] = (
            lambda: cast(CalendarService, app.state.calendar_service)
        )
        app.dependency_overrides[get_orders_broadcaster] = (
            lambda: cast(OrdersBroadcaster, app.state.orders_broadcaster)
        )
        app.dependency_overrides[get_orders_bootstrap_loader] = (
            lambda: cast(
                OrdersBootstrapLoader, app.state.orders_bootstrap_loader
            )
        )
        app.dependency_overrides[get_portfolio_service] = (
            lambda: cast(PortfolioService, app.state.portfolio_service)
        )
        app.dependency_overrides[get_orders_service] = (
            lambda: cast(OrdersService, app.state.orders_service)
        )
        app.dependency_overrides[get_idempotency_store] = (
            lambda: cast(IdempotencyStore, app.state.idempotency_store)
        )
        app.dependency_overrides[get_orders_portfolio_invalidator] = (
            lambda: cast(PortfolioService, app.state.portfolio_service)
        )
        app.dependency_overrides[get_trades_service] = (
            lambda: cast(TradesService, app.state.trades_service)
        )
        app.dependency_overrides[get_event_bus] = (
            lambda: cast(EventBus, app.state.event_bus)
        )

        await cp_lifecycle.start()

        # WS-Lifespan-Verdrahtung (AP-11 K9): opt-in via BG_QUOTES_SOURCE=ws.
        # Setzt voraus, dass cp_lifecycle nach dem ersten Tickle eine
        # session_id liefert und den Status auf OK gesetzt hat. Bei
        # Fallback bleibt der Polling-Pfad unveraendert.
        sub_manager, status_probe, ws_client, ws_source = await _build_subscription_layer(
            services_client=cast(CPGatewayClient, services_client),
            cp_lifecycle=cp_lifecycle,
            inst_service=inst_service,
            cal_service=cal_service,
            override_manager=subscription_manager,
        )

        actual_metrics.attach_live_collector(
            make_live_collector(
                lifecycle_snapshot=cp_lifecycle.snapshot,
                subscription_count=lambda: len(sub_manager.active_conids),
                throttle_metrics=actual_throttle.metrics,
            )
        )
        app.state.subscription_manager = sub_manager
        app.state.status_probe = status_probe
        app.state.ws_client = ws_client
        app.state.ws_source = ws_source
        app.dependency_overrides[get_subscription_manager] = (
            lambda: cast(SubscriptionManager, app.state.subscription_manager)
        )
        app.dependency_overrides[get_status_probe] = (
            lambda: cast(StatusProbe, app.state.status_probe)
        )
        try:
            yield
        finally:
            await evt_bus.shutdown()
            if ws_source is not None:
                try:
                    await ws_source.stop()
                except Exception:  # noqa: BLE001
                    _logger.warning("WSPushSource.stop fehlgeschlagen", exc_info=True)
            if ws_client is not None:
                try:
                    await ws_client.aclose()
                except Exception:  # noqa: BLE001
                    _logger.warning("CPWebSocketClient.aclose fehlgeschlagen", exc_info=True)
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
    app.add_middleware(ObservabilityMiddleware, metrics=actual_metrics)
    _install_error_handlers(app)
    app.include_router(v1_router)
    app.include_router(metrics_router)

    actual_store = store if store is not None else build_default_store()
    _ensure_bootstrap_admin(actual_store)
    app.state.token_store = actual_store
    app.state.metrics = actual_metrics
    app.state.throttle_manager = actual_throttle_outer
    app.dependency_overrides[get_token_store] = lambda: cast(TokenStore, app.state.token_store)
    app.dependency_overrides[get_metrics] = lambda: cast(BrokerGatewayMetrics, app.state.metrics)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    """Globale Handler, die alle Fehler in das v1-Section-1.6-Schema giessen."""

    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        code: str | None = None
        message: str
        retry_after_s: int | None = None
        extra: dict | None = None
        if isinstance(detail, dict):
            code = detail.get("code")
            message = str(
                detail.get("message")
                or detail.get("detail")
                or default_code_for(exc.status_code)
            )
            retry_after_s = detail.get("retry_after_s")
            extra = {
                k: v
                for k, v in detail.items()
                if k not in {"code", "message", "detail", "retry_after_s"}
            } or None
        else:
            message = str(detail) if detail else default_code_for(exc.status_code)
        # Retry-After-Header kann auch von HTTPException.headers kommen.
        if retry_after_s is None and exc.headers:
            raw = exc.headers.get("Retry-After") or exc.headers.get("retry-after")
            if raw is not None:
                try:
                    retry_after_s = int(raw)
                except (TypeError, ValueError):
                    retry_after_s = None
        return build_error_response(
            status_code=exc.status_code,
            code=code,
            message=message,
            request_id=getattr(request.state, "request_id", None),
            retry_after_s=retry_after_s,
            extra=extra,
            headers=exc.headers,
        )

    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        # Pydantic-Errors enthalten ggf. native Exceptions in `ctx.error`,
        # die nicht JSON-serialisierbar sind - in str() umwandeln.
        cleaned: list[dict] = []
        for err in exc.errors():
            entry = {k: v for k, v in err.items() if k != "ctx"}
            ctx = err.get("ctx")
            if isinstance(ctx, dict):
                entry["ctx"] = {ck: str(cv) for ck, cv in ctx.items()}
            cleaned.append(entry)
        return build_error_response(
            status_code=422,
            code="invalid_input",
            message="Request body or query parameters konnten nicht validiert werden",
            request_id=getattr(request.state, "request_id", None),
            extra={"errors": cleaned},
        )

    async def _cp_gateway_error_handler(request: Request, exc: CPGatewayError):
        extra: dict = {}
        if exc.upstream_status is not None:
            extra["upstream_status"] = exc.upstream_status
        if exc.upstream_code is not None:
            extra["upstream_code"] = exc.upstream_code
        return build_error_response(
            status_code=exc.status_code,
            code="cp_upstream_error",
            message=exc.message,
            request_id=getattr(request.state, "request_id", None),
            retry_after_s=exc.retry_after_s,
            extra=extra or None,
        )

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(CPGatewayError, _cp_gateway_error_handler)


app = create_app()
