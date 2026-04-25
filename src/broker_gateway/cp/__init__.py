"""CP-Gateway-Adapter (interner IBKR Client Portal Gateway-Client)."""
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import (
    Instrument,
    InstrumentDetail,
    InstrumentsService,
)
from broker_gateway.cp.lifecycle import (
    AuthLifecycle,
    AuthStatus,
    LifecycleSnapshot,
    require_session_ok,
)
from broker_gateway.cp.orders import OrdersService
from broker_gateway.cp.portfolio import (
    Ledger,
    LedgerEntry,
    PortfolioService,
    PortfolioSummary,
    Position,
)
from broker_gateway.cp.quotes import (
    FIELD_ALIASES,
    Quote,
    QuotesService,
    resolve_fields,
)

__all__ = [
    "CPGatewayClient",
    "Instrument",
    "InstrumentDetail",
    "InstrumentsService",
    "AuthLifecycle",
    "AuthStatus",
    "LifecycleSnapshot",
    "require_session_ok",
    "Ledger",
    "LedgerEntry",
    "OrdersService",
    "PortfolioService",
    "PortfolioSummary",
    "Position",
    "FIELD_ALIASES",
    "Quote",
    "QuotesService",
    "resolve_fields",
]
