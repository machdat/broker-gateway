from fastapi import FastAPI

from broker_gateway import __version__
from broker_gateway.api.v1 import router as v1_router

app = FastAPI(
    title="broker-gateway",
    version=__version__,
    description="Versionierte HTTP-API fuer broker-vermittelten Aktienhandel und Marktdaten-Streaming.",
)

app.include_router(v1_router)
