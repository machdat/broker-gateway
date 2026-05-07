"""Tests fuer den Cookie-Jar des CPGatewayClient (Karte 406fce15).

Phase A: httpx-Cookie-Jar — Cookies aus Set-Cookie-Headern landen
automatisch im Jar des Clients und werden bei nachfolgenden Requests
mitgesendet.

Phase B: Cookie-Path-Override — Set-Cookie mit Path=/sso wird auf
Path=/ umgeschrieben, damit die Session-Cookies auch bei /v1/api/*
matchen. Tests dafuer kommen in einer separaten Klasse, sobald die
Hook-Logik implementiert ist.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from broker_gateway.cp.client import CPGatewayClient


_BASE_URL = "http://cpgateway:5000/v1/api"


@pytest.mark.asyncio
async def test_cookie_jar_default_is_empty() -> None:
    client = CPGatewayClient(base_url=_BASE_URL)
    try:
        assert isinstance(client.cookies, httpx.Cookies)
        assert dict(client.cookies) == {}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cookie_jar_can_be_seeded_via_constructor() -> None:
    seeded = httpx.Cookies()
    seeded.set("JSESSIONID", "abc123")
    client = CPGatewayClient(base_url=_BASE_URL, cookies=seeded)
    try:
        # httpx kopiert seed-Cookies in seinen internen Jar; die
        # Property zeigt auf den Client-Jar (shared state).
        assert client.cookies.get("JSESSIONID") == "abc123"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cookie_jar_absorbs_set_cookie_from_response(respx_mock: respx.MockRouter) -> None:
    """httpx hinterlegt Set-Cookie-Header automatisch im Jar.

    Der Test verifiziert, dass das auch durch unsere Wrapper-Schicht
    sichtbar wird — sprich der von ``client.cookies`` exponierte Jar
    enthaelt nach dem Roundtrip die Server-Cookie.
    """
    respx_mock.get(f"{_BASE_URL}/iserver/auth/status").respond(
        status_code=200,
        json={"authenticated": True},
        headers={"Set-Cookie": "JSESSIONID=fromserver; Path=/sso"},
    )

    client = CPGatewayClient(base_url=_BASE_URL)
    try:
        await client.auth_status()
        # httpx fuegt unter Domain "cpgateway" ein. Wir checken, dass
        # die Cookie ueberhaupt im Jar liegt — der Path-Override-Test
        # in Phase B prueft danach, dass Path=/ statt /sso ist.
        cookie_value = None
        for cookie in client.cookies.jar:
            if cookie.name == "JSESSIONID":
                cookie_value = cookie.value
                break
        assert cookie_value == "fromserver"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cookie_jar_sends_cookie_on_subsequent_request(respx_mock: respx.MockRouter) -> None:
    """Cookies aus dem Jar werden bei nachfolgenden Requests gesendet.

    httpx setzt automatisch die Domain auf den Request-Host, wenn der
    Cookie ohne explizite Domain im Jar liegt. Der Phase-B-Hook
    schreibt cpgateway-Cookies analog so um, dass sie ueber alle
    Pfade matchen.
    """
    client = CPGatewayClient(base_url=_BASE_URL)
    try:
        client.cookies.set("JSESSIONID", "preset-session")
        route = respx_mock.get(f"{_BASE_URL}/iserver/auth/status").respond(
            status_code=200,
            json={"authenticated": True},
        )
        await client.auth_status()
        sent_cookie_header = route.calls.last.request.headers.get("Cookie", "")
        assert "JSESSIONID=preset-session" in sent_cookie_header
    finally:
        await client.aclose()


# ---------- Phase B: Cookie-Path-Override-Hook ----------


@pytest.mark.asyncio
async def test_path_override_rewrites_sso_cookies_to_root(respx_mock: respx.MockRouter) -> None:
    """Set-Cookie mit Path=/sso landet als Path='/' im Jar.

    Der Hook reagiert auf alle Cookies, deren Domain dem Request-Host
    entspricht und deren Path != '/' ist.
    """
    respx_mock.get(f"{_BASE_URL}/iserver/auth/status").respond(
        status_code=200,
        json={"authenticated": True},
        headers={"Set-Cookie": "JSESSIONID=fromserver; Path=/sso"},
    )
    client = CPGatewayClient(base_url=_BASE_URL)
    try:
        await client.auth_status()
        # Suche den Cookie und pruefe den Pfad.
        jar_cookies = list(client.cookies.jar)
        jsessionid = next((c for c in jar_cookies if c.name == "JSESSIONID"), None)
        assert jsessionid is not None
        assert jsessionid.path == "/", f"Erwartet Path='/', bekam '{jsessionid.path}'"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_path_override_handles_multiple_cookies(respx_mock: respx.MockRouter) -> None:
    """Mehrere Set-Cookie-Header in einer Response — alle mit Path=/sso —
    werden samtlich auf Path='/' umgeschrieben.
    """
    respx_mock.get(f"{_BASE_URL}/iserver/auth/status").respond(
        status_code=200,
        json={"authenticated": True},
        headers=[
            ("Set-Cookie", "JSESSIONID=session-abc; Path=/sso"),
            ("Set-Cookie", "x-sess-uuid=uuid-def; Path=/sso"),
        ],
    )
    client = CPGatewayClient(base_url=_BASE_URL)
    try:
        await client.auth_status()
        jar_cookies = list(client.cookies.jar)
        names_paths = {c.name: c.path for c in jar_cookies}
        assert names_paths.get("JSESSIONID") == "/"
        assert names_paths.get("x-sess-uuid") == "/"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_path_override_leaves_root_path_untouched(respx_mock: respx.MockRouter) -> None:
    """Cookies, die schon Path='/' haben, werden nicht angetastet."""
    respx_mock.get(f"{_BASE_URL}/iserver/auth/status").respond(
        status_code=200,
        json={"authenticated": True},
        headers={"Set-Cookie": "PRESERVED=value; Path=/"},
    )
    client = CPGatewayClient(base_url=_BASE_URL)
    try:
        await client.auth_status()
        jar_cookies = list(client.cookies.jar)
        preserved = next((c for c in jar_cookies if c.name == "PRESERVED"), None)
        assert preserved is not None
        assert preserved.path == "/"
        # Sicherstellen, dass keine Duplikate entstanden sind.
        assert sum(1 for c in jar_cookies if c.name == "PRESERVED") == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sso_cookie_is_sent_on_v1_api_request(respx_mock: respx.MockRouter) -> None:
    """Roundtrip-Test: Server sendet Cookie mit Path=/sso, naechster
    /v1/api-Request enthaelt den Cookie-Header.

    Das ist die Kernverifikation fuer Phase B — ohne Override wuerde
    der zweite Request keinen Cookie mitsenden, weil /v1/api/tickle
    nicht zu /sso passt.
    """
    auth_status = respx_mock.get(f"{_BASE_URL}/iserver/auth/status").respond(
        status_code=200,
        json={"authenticated": True},
        headers={"Set-Cookie": "JSESSIONID=session-xyz; Path=/sso"},
    )
    tickle = respx_mock.post(f"{_BASE_URL}/tickle").respond(
        status_code=200,
        json={"session": "session-xyz", "userId": 1, "iserver": {"authStatus": {"authenticated": True}}},
    )
    client = CPGatewayClient(base_url=_BASE_URL)
    try:
        await client.auth_status()
        await client.tickle()
        sent_cookie_header = tickle.calls.last.request.headers.get("Cookie", "")
        assert "JSESSIONID=session-xyz" in sent_cookie_header, (
            f"Cookie-Header bei /tickle leer trotz Path-Override: {sent_cookie_header!r}"
        )
        # Sanity: auth_status hat den Cookie noch nicht gesendet (er kam
        # ja erst in der Response). Erst der zweite Call zeigt den Override.
        assert "JSESSIONID" not in auth_status.calls.last.request.headers.get("Cookie", "")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_path_override_only_affects_request_host(respx_mock: respx.MockRouter) -> None:
    """Cookies, die fuer einen anderen Host im Jar liegen, werden nicht
    angefasst — auch wenn ihr Pfad != '/' ist.
    """
    # Cookie fuer einen fremden Host vorab in den Jar packen.
    client = CPGatewayClient(base_url=_BASE_URL)
    try:
        # Direktes Einsetzen ueber den http.cookiejar-Cookie:
        import http.cookiejar
        foreign_cookie = http.cookiejar.Cookie(
            version=0, name="OTHER", value="foreign-value",
            port=None, port_specified=False,
            domain="other-host", domain_specified=True, domain_initial_dot=False,
            path="/legacy", path_specified=True,
            secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        )
        client.cookies.jar.set_cookie(foreign_cookie)

        respx_mock.get(f"{_BASE_URL}/iserver/auth/status").respond(
            status_code=200,
            json={"authenticated": True},
            headers={"Set-Cookie": "JSESSIONID=cp-session; Path=/sso"},
        )
        await client.auth_status()

        jar_cookies = list(client.cookies.jar)
        # Fremder Host darf nicht angetastet sein.
        other = next((c for c in jar_cookies if c.name == "OTHER"), None)
        assert other is not None
        assert other.path == "/legacy"
        # cpgateway-Cookie darf trotzdem auf Path='/' gepatcht sein.
        cp_cookie = next((c for c in jar_cookies if c.name == "JSESSIONID"), None)
        assert cp_cookie is not None
        assert cp_cookie.path == "/"
    finally:
        await client.aclose()
