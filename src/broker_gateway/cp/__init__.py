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

__all__ = [
    "CPGatewayClient",
    "Instrument",
    "InstrumentDetail",
    "InstrumentsService",
    "AuthLifecycle",
    "AuthStatus",
    "LifecycleSnapshot",
    "require_session_ok",
]
