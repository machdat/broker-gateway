"""Pytest-Fixtures fuer den Paper-Test-Stack (AP-07 K2).

Alle Tests unter ``tests_paper/`` sprechen ueber HTTP gegen einen
deployed broker-gateway-paper. Der Default-Repo-pytest-Lauf
ueberspringt die Suite (siehe ``pyproject.toml`` ``addopts``); nur
ein expliziter ``-m paper_<level>``-Aufruf laesst sie laufen.

Fixtures (alle Session-Scope, ausser dem HTTP-Client):

- ``paper_base_url`` - URL der deployed Paper-Instanz.
- ``paper_admin_token`` - Bootstrap-Admin-Token.
- ``paper_account_id`` - DU-Konto-ID, gegen die Whitelist geprueft.
- ``paper_session_warmup`` - autouse, macht zwei Pre-Flight-GETs.
- ``paper_http_client`` - httpx.AsyncClient mit Authorization-Header.

Hook ``pytest_collection_modifyitems`` skippt die Suite, wenn
``BG_PAPER_TESTS_DISABLED=true`` gesetzt ist.
"""
from __future__ import annotations

import os
from typing import Iterator

import httpx
import pytest


# ---------------------------------------------------------------------------
# Kill-Switch
# ---------------------------------------------------------------------------


_PAPER_MARKERS = {
    "paper_readonly",
    "paper_safe_write",
    "paper_pic",
    "paper_destructive",
}


def _is_paper_test(item: pytest.Item) -> bool:
    return any(item.get_closest_marker(name) for name in _PAPER_MARKERS)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    raw = os.environ.get("BG_PAPER_TESTS_DISABLED", "").lower()
    if raw not in ("1", "true", "yes"):
        return
    skip = pytest.mark.skip(
        reason="BG_PAPER_TESTS_DISABLED gesetzt - Paper-Suite deaktiviert."
    )
    for item in items:
        if _is_paper_test(item):
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


def _require_env(name: str, *, hint: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"{name} nicht gesetzt - {hint}",
            allow_module_level=False,
        )
    return value


@pytest.fixture(scope="session")
def paper_base_url() -> str:
    return _require_env(
        "BG_PAPER_BASE_URL",
        hint="z.B. http://cma-pi-1:4001 (siehe paper-account-setup.md)",
    )


@pytest.fixture(scope="session")
def paper_admin_token() -> str:
    return _require_env(
        "BG_PAPER_BOOTSTRAP_TOKEN",
        hint="Wert aus .env.paper BG_BOOTSTRAP_ADMIN_TOKEN.",
    )


@pytest.fixture(scope="session")
def paper_account_id() -> str:
    raw = os.environ.get("BG_PAPER_ACCOUNT_ID")
    if not raw:
        pytest.skip(
            "BG_PAPER_ACCOUNT_ID nicht gesetzt - DU-Konto-ID erforderlich."
        )
    # Lazy-Import, damit der DSL-Layer Tests-Ordner-spezifisch bleibt.
    from tests_paper._dsl.safety import (  # noqa: PLC0415
        PaperSafetyError,
        assert_paper_account,
    )

    try:
        return assert_paper_account(raw)
    except PaperSafetyError as exc:
        pytest.exit(
            f"Paper-Tests abgebrochen: {exc}",
            returncode=2,
        )


# ---------------------------------------------------------------------------
# Pre-Flight + HTTP-Client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def paper_session_warmup(
    paper_base_url: str, paper_admin_token: str
) -> Iterator[None]:
    """Macht zwei Pre-Flight-GETs gegen die Paper-Instanz.

    Bei Fehlern wird die Test-Session abgebrochen (``pytest.exit``),
    nicht skip - eine kaputte Paper-Instanz soll nicht still
    durchrutschen.
    """
    try:
        with httpx.Client(timeout=10.0) as client:
            health = client.get(f"{paper_base_url}/v1/health")
            if health.status_code != 200:
                pytest.exit(
                    "Paper-Pre-Flight: GET /v1/health -> "
                    f"HTTP {health.status_code}",
                    returncode=2,
                )
            internal = client.get(
                f"{paper_base_url}/v1/internal/health",
                headers={"Authorization": f"Bearer {paper_admin_token}"},
            )
            if internal.status_code != 200:
                pytest.exit(
                    "Paper-Pre-Flight: GET /v1/internal/health -> "
                    f"HTTP {internal.status_code}",
                    returncode=2,
                )
    except httpx.HTTPError as exc:
        pytest.exit(
            f"Paper-Pre-Flight: Verbindung zu {paper_base_url} "
            f"fehlgeschlagen: {exc}",
            returncode=2,
        )
    yield


@pytest.fixture
async def paper_http_client(
    paper_base_url: str, paper_admin_token: str
) -> Iterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=paper_base_url,
        timeout=15.0,
        headers={"Authorization": f"Bearer {paper_admin_token}"},
    ) as client:
        yield client


@pytest.fixture
async def paper_actions(
    paper_http_client: httpx.AsyncClient, paper_account_id: str
):
    """Vorgeladenes ``PaperActions``-DSL-Objekt (AP-07 K5)."""
    from tests_paper._dsl.actions import PaperActions  # noqa: PLC0415

    return PaperActions(paper_http_client, account_id=paper_account_id)
