from fastapi import APIRouter

from broker_gateway.api.v1.auth import router as auth_router
from broker_gateway.api.v1.health import router as health_router
from broker_gateway.api.v1.instruments import router as instruments_router
from broker_gateway.api.v1.internal_health import router as internal_health_router
from broker_gateway.api.v1.quotes import router as quotes_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(auth_router)
router.include_router(internal_health_router)
router.include_router(instruments_router)
router.include_router(quotes_router)
