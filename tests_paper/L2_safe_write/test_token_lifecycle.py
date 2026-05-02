"""L2 paper_safe_write: Token-Lifecycle gegen den Paper-Stack
(AP-12 L2-1).

Drei Pfade werden geprueft:

1. Admin erzeugt Token, Token wird als Bearer akzeptiert.
2. Admin erzeugt Token, der Token revoked sich selbst, danach 401.
3. Admin revoked einen Fremd-Token; ohne admin:* darf das nicht.

Cleanup-Disziplin: jeder Test revoked seinen erzeugten Token im
``finally``-Block. Damit bleiben keine offenen Tokens im Paper-Stack
zurueck.

Aufruf:

    BG_PAPER_BASE_URL=http://cma-pi-1:4001 \\
    BG_PAPER_BOOTSTRAP_TOKEN=<admin-token> \\
    BG_PAPER_ACCOUNT_ID=DUP799747 \\
    pytest -m paper_safe_write tests_paper/L2_safe_write/

(Der ``paper_safe_write``-Marker ist in pyproject.toml registriert
und vom Default-pytest-Lauf ausgeschlossen - Tests laufen nur, wenn
``-m paper_safe_write`` explizit gesetzt ist.)
"""
from __future__ import annotations

import httpx
import pytest

from tests_paper._dsl.assertions import assert_error_envelope_v1


pytestmark = pytest.mark.paper_safe_write


async def _create_token(
    client: httpx.AsyncClient, *, caller_id: str, scopes: list[str]
) -> dict[str, object]:
    response = await client.post(
        "/v1/auth/token",
        json={"caller_id": caller_id, "scopes": scopes},
    )
    assert response.status_code == 201, (
        f"create_token: erwartet 201, bekam {response.status_code} "
        f"(body={response.text!r})"
    )
    body = response.json()
    assert "value" in body and isinstance(body["value"], str), (
        f"create_token: 'value' fehlt im Body, bekam {body!r}"
    )
    return body


async def _revoke_token(
    client: httpx.AsyncClient,
    *,
    bearer: str,
    target_value: str | None = None,
) -> int:
    """``DELETE /v1/auth/token`` mit explizitem Bearer.

    ``target_value=None`` revoked das Bearer-Token selbst; sonst wird
    via Query-Param ``value`` ein Fremd-Token revoked. Der zurueck-
    gegebene Status ist nur informativ - der Aufrufer entscheidet, was
    er erwartet.
    """
    params: dict[str, str] = {}
    if target_value is not None:
        params["value"] = target_value
    response = await client.request(
        "DELETE",
        "/v1/auth/token",
        params=params,
        headers={"Authorization": f"Bearer {bearer}"},
    )
    return response.status_code


async def test_create_token_with_admin_scope_then_use_as_bearer(
    paper_http_client: httpx.AsyncClient,
    paper_admin_token: str,
) -> None:
    """Admin erzeugt einen quotes:read-Token; Token wird als Bearer
    auf /v1/instruments akzeptiert."""
    created = await _create_token(
        paper_http_client,
        caller_id="paper-l2-test-create",
        scopes=["instruments:read"],
    )
    new_value = created["value"]
    try:
        # Verifikation: der frische Token ist als Bearer brauchbar.
        verify = await paper_http_client.get(
            "/v1/instruments/search",
            params={"symbol": "AAPL"},
            headers={"Authorization": f"Bearer {new_value}"},
        )
        assert verify.status_code == 200, (
            f"frischer Token nicht akzeptiert: HTTP {verify.status_code} "
            f"(body={verify.text!r})"
        )
    finally:
        # Cleanup: Admin revoked den Test-Token.
        revoke_status = await _revoke_token(
            paper_http_client,
            bearer=paper_admin_token,
            target_value=str(new_value),
        )
        assert revoke_status in (204, 404), (
            f"cleanup-revoke: erwartet 204/404, bekam {revoke_status}"
        )


async def test_revoke_self_with_own_bearer(
    paper_http_client: httpx.AsyncClient,
) -> None:
    """Token revoked sich selbst; danach liefert er 401."""
    created = await _create_token(
        paper_http_client,
        caller_id="paper-l2-test-revoke-self",
        scopes=["instruments:read"],
    )
    new_value = str(created["value"])
    revoked = False
    try:
        # Self-Revoke (kein value-Query-Param -> revoked das Bearer).
        status = await _revoke_token(paper_http_client, bearer=new_value)
        assert status == 204, f"self-revoke: erwartet 204, bekam {status}"
        revoked = True

        # Verifikation: Token ist tot.
        verify = await paper_http_client.get(
            "/v1/instruments/search",
            params={"symbol": "AAPL"},
            headers={"Authorization": f"Bearer {new_value}"},
        )
        assert verify.status_code == 401, (
            f"revoker Token darf nicht mehr funktionieren: HTTP "
            f"{verify.status_code}"
        )
    finally:
        if not revoked:
            # Best-effort-Cleanup, falls der Self-Revoke schiefging.
            await _revoke_token(
                paper_http_client,
                bearer=paper_http_client.headers["Authorization"].removeprefix(
                    "Bearer "
                ),
                target_value=new_value,
            )


async def test_create_without_admin_scope_returns_403(
    paper_http_client: httpx.AsyncClient,
    paper_admin_token: str,
) -> None:
    """Ein normaler instruments:read-Token darf KEIN Token erzeugen
    (nur admin:* darf das)."""
    helper = await _create_token(
        paper_http_client,
        caller_id="paper-l2-test-no-admin",
        scopes=["instruments:read"],
    )
    helper_value = str(helper["value"])
    try:
        async with httpx.AsyncClient(
            base_url=str(paper_http_client.base_url),
            headers={"Authorization": f"Bearer {helper_value}"},
            timeout=15.0,
        ) as non_admin:
            response = await non_admin.post(
                "/v1/auth/token",
                json={
                    "caller_id": "should-fail",
                    "scopes": ["instruments:read"],
                },
            )
        assert response.status_code == 403, (
            f"non-admin create_token: erwartet 403, bekam "
            f"{response.status_code}"
        )
        assert_error_envelope_v1(response, expected_code="missing_scope")
    finally:
        await _revoke_token(
            paper_http_client,
            bearer=paper_admin_token,
            target_value=helper_value,
        )


async def test_revoke_foreign_without_admin_returns_403(
    paper_http_client: httpx.AsyncClient,
    paper_admin_token: str,
) -> None:
    """Ein nicht-admin-Token darf KEINEN fremden Token revoken."""
    target = await _create_token(
        paper_http_client,
        caller_id="paper-l2-test-foreign-target",
        scopes=["instruments:read"],
    )
    helper = await _create_token(
        paper_http_client,
        caller_id="paper-l2-test-foreign-helper",
        scopes=["instruments:read"],
    )
    target_value = str(target["value"])
    helper_value = str(helper["value"])
    try:
        async with httpx.AsyncClient(
            base_url=str(paper_http_client.base_url),
            headers={"Authorization": f"Bearer {helper_value}"},
            timeout=15.0,
        ) as non_admin:
            response = await non_admin.request(
                "DELETE",
                "/v1/auth/token",
                params={"value": target_value},
            )
        assert response.status_code == 403, (
            f"non-admin revoke-foreign: erwartet 403, bekam "
            f"{response.status_code}"
        )
    finally:
        # Cleanup-Reihenfolge egal; admin revoked beide.
        await _revoke_token(
            paper_http_client,
            bearer=paper_admin_token,
            target_value=target_value,
        )
        await _revoke_token(
            paper_http_client,
            bearer=paper_admin_token,
            target_value=helper_value,
        )
