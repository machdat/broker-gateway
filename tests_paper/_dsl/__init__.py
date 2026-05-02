"""DSL fuer Paper-Tests - safety/assertions/actions als Modul-Layer."""
from tests_paper._dsl.actions import (
    PaperActions,
    cancel_all_open_orders,
    cancel_order,
    flatten_positions,
    place_limit_far_from_market,
    subscribe_quote_stream,
    wait_for_order_status,
)
from tests_paper._dsl.assertions import (
    assert_error_envelope_v1,
    assert_event_envelope_v1,
    assert_idempotency_replay_returns_original,
    assert_money_close,
    assert_money_normalized,
    assert_order_status_in,
    assert_pacing_headers_present,
    assert_side_valid,
)
from tests_paper._dsl.safety import (
    PaperSafetyError,
    assert_paper_account,
    assert_within_paper_limits,
    kill_switch_active,
    max_notional_per_order,
    max_open_orders,
)

__all__ = [
    "PaperActions",
    "PaperSafetyError",
    "assert_error_envelope_v1",
    "assert_event_envelope_v1",
    "assert_idempotency_replay_returns_original",
    "assert_money_close",
    "assert_money_normalized",
    "assert_order_status_in",
    "assert_paper_account",
    "assert_pacing_headers_present",
    "assert_side_valid",
    "assert_within_paper_limits",
    "cancel_all_open_orders",
    "cancel_order",
    "flatten_positions",
    "kill_switch_active",
    "max_notional_per_order",
    "max_open_orders",
    "place_limit_far_from_market",
    "subscribe_quote_stream",
    "wait_for_order_status",
]
