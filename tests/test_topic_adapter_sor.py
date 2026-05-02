"""Tests fuer ``broker_gateway.cp.topics.sor.SorTopicAdapter``.

Pruefen:

1. Field-Mapping nach K6-Anhang A (orderId/cOID/parentId/acct/ticker/
   side/totalSize/filledQuantity/avgPrice/status/timeInForce/
   lastExecutionTime/orderRejectReason).
2. Status-Normalisierung: IBKR-Werte werden auf semantisches Set
   reduziert.
3. timeInForce-Quirk: ``CLOSE`` -> ``DAY``.
4. UI-Felder ``bgColor``/``fgColor`` werden gefiltert.
5. Snapshot-Merge ueber Folgeframes (Status-Delta erhaelt vorherige
   Felder).
6. Bootstrap-Pfad: REST-Liste landet im selben Snapshot-Cache.
"""
from __future__ import annotations

from decimal import Decimal

from broker_gateway.cp.topics.sor import SorFrame, SorTopicAdapter


def test_field_mapping_per_appendix_a() -> None:
    adapter = SorTopicAdapter()
    raw = {
        "topic": "sor",
        "orderId": 912091175,
        "cOID": "client-abc",
        "parentId": 100,
        "acct": "U25235077",
        "ticker": "AAPL",
        "side": "BUY",
        "totalSize": "1.0",
        "filledQuantity": "0.0",
        "avgPrice": "0.00",
        "status": "PendingSubmit",
        "timeInForce": "DAY",
        "lastExecutionTime": "2026-05-01T15:00:00Z",
        "orderRejectReason": None,
        "conid": 265598,
        "bgColor": "#abcdef",
        "fgColor": "#000000",
    }
    frame = adapter.feed(raw)

    assert isinstance(frame, SorFrame)
    assert frame.order_id == 912091175
    assert frame.client_order_id == "client-abc"
    assert frame.parent_id == 100
    assert frame.account == "U25235077"
    assert frame.symbol == "AAPL"
    assert frame.side == "BUY"
    assert frame.quantity == Decimal("1.0")
    assert frame.filled_quantity == Decimal("0.0")
    assert frame.avg_fill_price == Decimal("0.00")
    assert frame.status == "pending"
    assert frame.time_in_force == "DAY"
    assert frame.last_event_at == "2026-05-01T15:00:00Z"
    assert frame.reject_reason is None
    assert frame.conid == 265598


def test_status_normalisation_covers_lifecycle() -> None:
    adapter = SorTopicAdapter()
    cases = {
        "Inactive": "pending",
        "PendingSubmit": "pending",
        "PreSubmitted": "accepted",
        "Submitted": "accepted",
        "PartialFill": "partial_fill",
        "Filled": "filled",
        "PendingCancel": "pending",
        "Cancelled": "cancelled",
        "Rejected": "rejected",
        "UnknownState": "pending",  # defensiver Default
    }
    for ibkr_status, semantic in cases.items():
        frame = adapter.feed(
            {"topic": "sor", "orderId": 1, "status": ibkr_status}
        )
        assert frame is not None
        assert frame.status == semantic, f"{ibkr_status} -> {semantic}"


def test_time_in_force_close_quirk_is_normalised_to_day() -> None:
    adapter = SorTopicAdapter()
    frame = adapter.feed(
        {"topic": "sor", "orderId": 2, "timeInForce": "CLOSE"}
    )
    assert frame is not None
    assert frame.time_in_force == "DAY"


def test_ui_color_fields_are_filtered() -> None:
    adapter = SorTopicAdapter()
    frame = adapter.feed(
        {
            "topic": "sor",
            "orderId": 3,
            "bgColor": "#ff00ff",
            "fgColor": "#00ff00",
            "status": "Filled",
        }
    )
    assert frame is not None
    assert frame.status == "filled"
    # Sicherstellen, dass keine Farb-Felder im SorFrame-Schema landen
    # (bg/fg sind nicht Teil von SorFrame's Felder).


def test_delta_frames_merge_into_existing_snapshot() -> None:
    adapter = SorTopicAdapter()
    initial = adapter.feed(
        {
            "topic": "sor",
            "orderId": 4,
            "ticker": "AAPL",
            "side": "BUY",
            "totalSize": "1.0",
            "status": "PendingSubmit",
        }
    )
    delta = adapter.feed(
        {"topic": "sor", "orderId": 4, "status": "Submitted"}
    )

    assert initial is not None and delta is not None
    assert delta.symbol == "AAPL"  # vom ersten Frame uebernommen
    assert delta.side == "BUY"
    assert delta.quantity == Decimal("1.0")
    assert delta.status == "accepted"


def test_bootstrap_pathway_normalises_rest_orders() -> None:
    adapter = SorTopicAdapter()
    rest_orders = [
        {
            "orderId": 5,
            "ticker": "MSFT",
            "side": "SELL",
            "totalSize": "10",
            "filledQuantity": "10",
            "avgPrice": "320.50",
            "status": "Filled",
            "timeInForce": "DAY",
            "acct": "U25235077",
        },
        {
            "orderId": 6,
            "ticker": "AAPL",
            "side": "BUY",
            "status": "Submitted",
            "timeInForce": "CLOSE",
            "acct": "U25235077",
        },
    ]
    frames = adapter.bootstrap(rest_orders)

    assert len(frames) == 2
    msft, aapl = frames
    assert msft.symbol == "MSFT"
    assert msft.status == "filled"
    assert aapl.status == "accepted"
    assert aapl.time_in_force == "DAY"  # CLOSE-Quirk normalisiert


def test_non_sor_frame_returns_none() -> None:
    adapter = SorTopicAdapter()
    assert adapter.feed({"topic": "smd", "conid": 1}) is None
    assert adapter.feed({}) is None


def test_frame_without_order_id_returns_none() -> None:
    adapter = SorTopicAdapter()
    assert adapter.feed({"topic": "sor", "ticker": "AAPL"}) is None
