"""Tests fuer ``GET /v1/status`` (AP-11 K8)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker_gateway.api.v1.status import StatusProbe
from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_INSTRUMENTS_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app
from broker_gateway.streams.registry import SubscriptionRegistry


_ADMIN_VALUE = "status-admin-token-aaaaaaaaaaaaaaaaaa"


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(
        Token(
            value=_ADMIN_VALUE,
            caller_id="bootstrap-admin",
            scopes=[SCOPE_ADMIN_ALL],
        )
    )
    return s


@pytest.fixture
async def cp_client(cp_gateway_mock):
    client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    yield client
    await client.aclose()


@pytest.fixture
async def lifecycle(cp_client):
    lc = AuthLifecycle(
        cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0
    )
    yield lc
    await lc.stop()


@pytest.fixture
async def client(store, lifecycle, cp_gateway_mock):
    app = create_app(store=store, lifecycle=lifecycle)
    with TestClient(app) as test_client:
        yield test_client


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_VALUE}"}


def test_status_endpoint_returns_schema(client: TestClient) -> None:
    response = client.get("/v1/status", headers=_auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "cp_gateway_connected",
        "last_frame_age_seconds",
        "reconnect_attempt",
        "subscriptions_active",
    }


def test_status_endpoint_reflects_no_frames_yet(client: TestClient) -> None:
    """Im Cold-Start ohne WSPushSource: kein Frame, kein Reconnect."""
    response = client.get("/v1/status", headers=_auth_headers())
    body = response.json()
    assert body["last_frame_age_seconds"] is None
    assert body["reconnect_attempt"] == 0
    assert body["subscriptions_active"] == 0


def test_status_endpoint_requires_instruments_scope(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    no_scope = "status-no-scope-aaaaaaaaaaaaaaaaaa"
    store.put(
        Token(value=no_scope, caller_id="other", scopes=[])
    )
    response = client.get(
        "/v1/status",
        headers={"Authorization": f"Bearer {no_scope}"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# StatusProbe-Unit-Tests (ohne FastAPI)
# ---------------------------------------------------------------------------


async def test_probe_mark_frame_resets_age_to_close_to_zero() -> None:
    probe = StatusProbe()
    probe.mark_frame()
    snap = probe.snapshot(_FakeLifecycle(connected=True))
    assert snap["cp_gateway_connected"] is True
    assert snap["last_frame_age_seconds"] is not None
    assert snap["last_frame_age_seconds"] < 0.5


async def test_probe_with_registry_count_reflects_active_subs() -> None:
    rec_calls: list[tuple[str, dict]] = []

    async def _subscribe(topic: str, args: dict) -> None:
        rec_calls.append((topic, args))

    registry = SubscriptionRegistry(subscribe=_subscribe)
    owner = object()
    await registry.add("smd", {"conid": 1}, owner)
    await registry.add("smd", {"conid": 2}, owner)

    probe = StatusProbe(registry=registry)
    snap = probe.snapshot(_FakeLifecycle(connected=False))
    assert snap["subscriptions_active"] == 2
    assert snap["cp_gateway_connected"] is False


async def test_probe_with_reconnect_callback_returns_attempt() -> None:
    state = {"attempt": 3}
    probe = StatusProbe(ws_reconnect_attempt=lambda: state["attempt"])
    snap = probe.snapshot(_FakeLifecycle(connected=True))
    assert snap["reconnect_attempt"] == 3


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------


class _FakeLifecycle:
    def __init__(self, *, connected: bool) -> None:
        self._connected = connected

    def snapshot(self):
        from broker_gateway.cp.lifecycle import (  # noqa: PLC0415
            AuthStatus,
            LifecycleSnapshot,
        )

        return LifecycleSnapshot(
            auth_status=AuthStatus.OK
            if self._connected
            else AuthStatus.AUTH_LOST,
            cp_reachable=self._connected,
            last_tickle_at=None,
            last_reauth_at=None,
            last_sso_validate_at=None,
            last_login_at=None,
            session_age_s=None,
            consecutive_reauth_failures=0,
            accounts_initialized=self._connected,
            session_id=None,
            iserver_bridge_ok=self._connected,
            last_bridge_probe_at=None,
            consecutive_bridge_failures=0,
        )
