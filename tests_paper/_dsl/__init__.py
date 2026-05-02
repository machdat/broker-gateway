"""DSL fuer Paper-Tests - safety/assertions/actions als Modul-Layer."""
from tests_paper._dsl.assertions import (
    assert_money_close,
    assert_order_status_in,
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
    "PaperSafetyError",
    "assert_money_close",
    "assert_order_status_in",
    "assert_paper_account",
    "assert_side_valid",
    "assert_within_paper_limits",
    "kill_switch_active",
    "max_notional_per_order",
    "max_open_orders",
]
