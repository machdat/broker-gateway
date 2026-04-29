"""Tests fuer das gemeinsame Header-Redaktion-Modul cp/redaction.py."""
from __future__ import annotations

import httpx
import pytest

from broker_gateway.cp.redaction import REDACTED_HEADERS, filter_headers


@pytest.mark.parametrize(
    "header_name",
    [
        "Authorization",
        "AUTHORIZATION",
        "authorization",
        "Cookie",
        "Set-Cookie",
        "set-cookie",
        "X-API-Key",
        "x-api-key",
        "X-Auth-Token",
        "Proxy-Authorization",
    ],
)
def test_filter_headers_strips_each_redacted_header(header_name: str) -> None:
    headers = {header_name: "SECRET", "Content-Type": "application/json"}
    result = filter_headers(headers)
    assert "Content-Type" in result
    assert all(name.lower() != header_name.lower() for name in result)


def test_filter_headers_keeps_unrelated_headers() -> None:
    headers = {
        "Authorization": "Bearer x",
        "Content-Type": "application/json",
        "X-Request-ID": "abc",
        "Accept": "*/*",
    }
    result = filter_headers(headers)
    assert result == {
        "Content-Type": "application/json",
        "X-Request-ID": "abc",
        "Accept": "*/*",
    }


def test_filter_headers_handles_httpx_headers() -> None:
    headers = httpx.Headers([
        ("Authorization", "Bearer secret-token"),
        ("Cookie", "csrftoken=foo"),
        ("X-Request-ID", "abc"),
    ])
    result = filter_headers(headers)
    assert "X-Request-ID" in result or "x-request-id" in result
    # httpx.Headers normalisiert Namen lower-case beim Iterieren.
    assert all(name.lower() not in REDACTED_HEADERS for name in result)


def test_filter_headers_handles_iterable_of_pairs() -> None:
    pairs = [
        ("Authorization", "Bearer x"),
        ("X-Custom", "value"),
    ]
    result = filter_headers(pairs)
    assert result == {"X-Custom": "value"}


def test_redacted_headers_are_lowercase_canonical() -> None:
    assert all(name == name.lower() for name in REDACTED_HEADERS)
