"""Unit-Tests fuer die fuenf v1-Vertrag-Asserts (AP-07 K4)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests_paper._dsl.assertions import (
    assert_error_envelope_v1,
    assert_event_envelope_v1,
    assert_idempotency_replay_returns_original,
    assert_money_normalized,
    assert_pacing_headers_present,
)


# ---------------------------------------------------------------------------
# assert_money_normalized
# ---------------------------------------------------------------------------


def test_money_normalized_accepts_canonical() -> None:
    assert_money_normalized({"value": Decimal("1.23"), "currency": "USD"})
    assert_money_normalized({"value": 100, "currency": "EUR"})
    assert_money_normalized({"value": "1.23", "currency": "GBP"})


def test_money_normalized_rejects_extra_field() -> None:
    with pytest.raises(AssertionError, match="unerwartete Felder"):
        assert_money_normalized(
            {"value": 1.0, "currency": "USD", "extra": "x"}
        )


def test_money_normalized_rejects_missing_value() -> None:
    with pytest.raises(AssertionError, match="value"):
        assert_money_normalized({"currency": "USD"})


def test_money_normalized_rejects_invalid_currency() -> None:
    with pytest.raises(AssertionError, match="currency"):
        assert_money_normalized({"value": 1.0, "currency": "us"})
    with pytest.raises(AssertionError, match="currency"):
        assert_money_normalized({"value": 1.0, "currency": "USDX"})


def test_money_normalized_rejects_non_dict() -> None:
    with pytest.raises(AssertionError, match="erwartet dict"):
        assert_money_normalized([1.0, "USD"])


# ---------------------------------------------------------------------------
# assert_event_envelope_v1
# ---------------------------------------------------------------------------


def test_event_envelope_accepts_canonical() -> None:
    assert_event_envelope_v1(
        {"data": {"x": 1}, "type": "execution", "id": "evt-1"}
    )
    assert_event_envelope_v1(
        {"data": {}, "type": "position", "id": 42, "retry": 1000}
    )


def test_event_envelope_rejects_unknown_type() -> None:
    with pytest.raises(AssertionError, match="Whitelist"):
        assert_event_envelope_v1(
            {"data": {}, "type": "ping", "id": "x"}
        )


def test_event_envelope_rejects_missing_field() -> None:
    with pytest.raises(AssertionError, match="data"):
        assert_event_envelope_v1({"type": "execution", "id": "x"})


def test_event_envelope_rejects_bad_id() -> None:
    with pytest.raises(AssertionError, match="id"):
        assert_event_envelope_v1(
            {"data": {}, "type": "execution", "id": [1, 2]}
        )


# ---------------------------------------------------------------------------
# assert_idempotency_replay_returns_original
# ---------------------------------------------------------------------------


def test_idempotency_replay_passes_for_identical_dicts() -> None:
    body = {"order_id": 12345, "status": "Submitted"}
    assert_idempotency_replay_returns_original(body, dict(body))


def test_idempotency_replay_fails_on_order_id_mismatch() -> None:
    with pytest.raises(AssertionError, match="order_id"):
        assert_idempotency_replay_returns_original(
            {"order_id": 1, "status": "ok"},
            {"order_id": 2, "status": "ok"},
        )


def test_idempotency_replay_fails_on_body_drift() -> None:
    with pytest.raises(AssertionError, match="Body-Identitaet"):
        assert_idempotency_replay_returns_original(
            {"order_id": 1, "status": "ok"},
            {"order_id": 1, "status": "Filled"},
        )


# ---------------------------------------------------------------------------
# assert_error_envelope_v1
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: dict, *, status_code: int = 400) -> None:
        self._body = body
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._body


def test_error_envelope_accepts_canonical() -> None:
    body = {
        "error": {
            "code": "missing_scope",
            "message": "Scope x fehlt",
            "request_id": "req-1",
        }
    }
    assert_error_envelope_v1(body)
    assert_error_envelope_v1(_FakeResponse(body))


def test_error_envelope_rejects_legacy_detail_format() -> None:
    with pytest.raises(AssertionError, match="error"):
        assert_error_envelope_v1({"detail": "Token fehlt"})


def test_error_envelope_rejects_missing_request_id() -> None:
    with pytest.raises(AssertionError, match="request_id"):
        assert_error_envelope_v1(
            {"error": {"code": "x", "message": "y"}}
        )


def test_error_envelope_expected_code_check() -> None:
    body = {
        "error": {
            "code": "missing_scope",
            "message": "Scope fehlt",
            "request_id": "req-1",
        }
    }
    assert_error_envelope_v1(body, expected_code="missing_scope")
    with pytest.raises(AssertionError, match="invalid_token"):
        assert_error_envelope_v1(body, expected_code="invalid_token")


# ---------------------------------------------------------------------------
# assert_pacing_headers_present
# ---------------------------------------------------------------------------


def test_pacing_headers_429_requires_retry_after() -> None:
    response = _FakeResponse({}, status_code=429)
    with pytest.raises(AssertionError, match="Retry-After"):
        assert_pacing_headers_present(response)

    response.headers = {"Retry-After": "30"}
    assert_pacing_headers_present(response)


def test_pacing_headers_200_no_requirements() -> None:
    response = _FakeResponse({}, status_code=200)
    assert_pacing_headers_present(response)
