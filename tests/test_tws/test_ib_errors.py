"""Tests fuer broker_gateway.tws.ib_errors (Karte dbc5edd7).

Gemeinsamer Helper fuer die ib_async-Fehlermechanik: mit
RaiseRequestErrors=False wirft ib_async bei IBKR-Request-Fehlern NICHT,
sondern loest das Future leer auf und emittiert den Fehler ueber
errorEvent. Der Helper kapselt das Sammeln der Events, den benign/fatal-
Filter und die reqId-Zuordnung - geteilt von Scanner und Historik.

Mock-Strategie wie test_scanner: ein echtes eventkit.Event als
errorEvent-Handle, damit der real relevante Pfad getestet wird.
"""
from __future__ import annotations

from types import SimpleNamespace

from eventkit import Event

from broker_gateway.tws.ib_errors import (
    IB_REQUEST_WARNING_CODES,
    collect_request_errors,
    first_fatal_ib_error,
    is_benign_ib_code,
)


# --------------------------------------------------------------------------
# is_benign_ib_code
# --------------------------------------------------------------------------


class TestIsBenignIbCode:
    def test_warning_codes_are_benign(self) -> None:
        for code in IB_REQUEST_WARNING_CODES:
            assert is_benign_ib_code(code) is True

    def test_165_no_results_is_benign(self) -> None:
        assert is_benign_ib_code(165) is True

    def test_2100_2199_system_messages_are_benign(self) -> None:
        assert is_benign_ib_code(2100) is True
        assert is_benign_ib_code(2104) is True
        assert is_benign_ib_code(2199) is True

    def test_2200_is_not_benign(self) -> None:
        assert is_benign_ib_code(2200) is False

    def test_pacing_162_is_fatal(self) -> None:
        assert is_benign_ib_code(162) is False

    def test_market_data_not_subscribed_10162_is_fatal(self) -> None:
        assert is_benign_ib_code(10162) is False


# --------------------------------------------------------------------------
# first_fatal_ib_error
# --------------------------------------------------------------------------


class TestFirstFatalIbError:
    def test_fatal_for_own_reqid(self) -> None:
        assert first_fatal_ib_error([(7, 162, "pacing")], 7) == (162, "pacing")

    def test_benign_165_returns_none(self) -> None:
        assert first_fatal_ib_error([(7, 165, "no results")], 7) is None

    def test_foreign_reqid_ignored(self) -> None:
        assert first_fatal_ib_error([(999, 162, "other request")], 7) is None

    def test_reqid_none_considers_all(self) -> None:
        assert first_fatal_ib_error([(5, 162, "pacing")], None) == (162, "pacing")

    def test_2100_2199_ignored(self) -> None:
        assert first_fatal_ib_error([(7, 2104, "farm connection ok")], 7) is None

    def test_first_fatal_wins_benign_skipped(self) -> None:
        errors = [(7, 165, "no results"), (7, 10162, "not subscribed")]
        assert first_fatal_ib_error(errors, 7) == (10162, "not subscribed")

    def test_empty_returns_none(self) -> None:
        assert first_fatal_ib_error([], 7) is None


# --------------------------------------------------------------------------
# collect_request_errors (Contextmanager)
# --------------------------------------------------------------------------


class TestCollectRequestErrors:
    def test_collects_emitted_events(self) -> None:
        ib = SimpleNamespace(errorEvent=Event("errorEvent"))
        with collect_request_errors(ib) as errors:
            ib.errorEvent.emit(7, 162, "pacing", None)
            ib.errorEvent.emit(8, 200, "no security definition", None)
        assert errors == [(7, 162, "pacing"), (8, 200, "no security definition")]

    def test_handler_detached_after_block(self) -> None:
        ib = SimpleNamespace(errorEvent=Event("errorEvent"))
        before = len(ib.errorEvent)
        with collect_request_errors(ib):
            pass
        assert len(ib.errorEvent) == before

    def test_handler_detached_even_on_exception(self) -> None:
        ib = SimpleNamespace(errorEvent=Event("errorEvent"))
        before = len(ib.errorEvent)
        try:
            with collect_request_errors(ib):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert len(ib.errorEvent) == before

    def test_events_after_block_not_collected(self) -> None:
        ib = SimpleNamespace(errorEvent=Event("errorEvent"))
        with collect_request_errors(ib) as errors:
            ib.errorEvent.emit(7, 162, "during", None)
        ib.errorEvent.emit(7, 200, "after", None)
        assert errors == [(7, 162, "during")]
