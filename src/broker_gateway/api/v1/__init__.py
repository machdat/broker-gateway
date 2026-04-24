from fastapi import APIRouter

from broker_gateway.api.v1.auth import router as auth_router
from broker_gateway.api.v1.health import router as health_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(auth_router)
