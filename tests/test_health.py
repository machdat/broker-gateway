from fastapi.testclient import TestClient

from broker_gateway import __version__
from broker_gateway.main import app


client = TestClient(app)


def test_health_returns_ok_and_version() -> None:
    response = client.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "version": __version__}


def test_health_version_matches_package_version() -> None:
    response = client.get("/v1/health")

    assert response.json()["version"] == "1.6.0"
