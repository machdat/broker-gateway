"""Tests fuer ``broker_gateway.cp.topics.smd.SmdTopicAdapter``.

Die Tests speisen synthetische ``smd``-Frames im IBKR-Wire-Format
(siehe ``docs/research/ibkr-cpapi-websockets-findings.md`` Sektion *a*)
in den Adapter und pruefen die Vier-Saeulen-Garantie:

1. Mixed-Type-Dekodierung (String-Preis -> Decimal, String-Size -> int,
   Float-Change-Pct -> float).
2. Erster Frame liefert Voll-Snapshot.
3. Folge-Delta-Frames mergen ins Snapshot, Werte aus dem ersten Frame
   bleiben erhalten, neue Werte ueberschreiben.
4. Dedup via ``(conid, _updated)`` (``tic``-Multiplikator-Schutz).

Plus zwei Smoke-Tests:

5. Replay gegen ``tests/fixtures/recorded/ws/spike-baseline.jsonl``
   (Baseline enthaelt keine ``smd``-Frames; der Adapter darf keinen
   einzigen ``SmdFrame`` produzieren - wichtige Negativ-Garantie).
6. Forward-Compat: unbekannte Field-IDs werden ignoriert, ohne den
   Decoder zu sprengen.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from broker_gateway.cp.topics.smd import SmdFrame, SmdTopicAdapter


CONID_AAPL = 265598


def _smd_first_frame() -> dict[str, Any]:
    """Erster Frame nach Subscribe gemaess Anhang B.

    String-Preis ``"271.55"``, Float-Change ``0.56``, String-Sizes
    (``"100"``, ``"200"``), int-Volume.
    """
    return {
        "topic": f"smd+{CONID_AAPL}",
        "conid": CONID_AAPL,
        "_updated": 1777415639001,
        "31": "271.55",
        "84": "271.50",
        "86": "271.60",
        "88": "100",
        "85": "200",
        "87": 1234567,
        "7059": "50",
        "83": 0.56,
        "6509": "DPB",
        "70": "275.00",
        "71": "268.40",
        "6119": "q1",
        "server_id": "q1",
    }


def _smd_delta_bidask() -> dict[str, Any]:
    """Delta-Frame: nur bid/ask + neuer Timestamp."""
    return {
        "topic": f"smd+{CONID_AAPL}",
        "conid": CONID_AAPL,
        "_updated": 1777415639002,
        "84": "271.51",
        "86": "271.59",
    }


# ---------------------------------------------------------------------------
# 1. Mixed-Type-Dekodierung
# ---------------------------------------------------------------------------


def test_first_frame_decodes_mixed_types_to_anhang_b_targets() -> None:
    adapter = SmdTopicAdapter()

    frame = adapter.feed(_smd_first_frame())

    assert isinstance(frame, SmdFrame)
    assert frame.conid == CONID_AAPL
    assert frame.last == Decimal("271.55")
    assert frame.bid == Decimal("271.50")
    assert frame.ask == Decimal("271.60")
    assert frame.bid_size == 100
    assert frame.ask_size == 200
    assert frame.volume == 1234567
    assert frame.last_size == 50
    assert frame.change_pct == pytest.approx(0.56)
    assert frame.availability_code == "DPB"
    assert frame.high == Decimal("275.00")
    assert frame.low == Decimal("268.40")
    assert frame.server_id == "q1"


# ---------------------------------------------------------------------------
# 2./3. Voll-Snapshot beim ersten Frame, Delta-Merge erhaelt alte Werte
# ---------------------------------------------------------------------------


def test_delta_frame_merges_into_existing_snapshot() -> None:
    adapter = SmdTopicAdapter()

    adapter.feed(_smd_first_frame())
    merged = adapter.feed(_smd_delta_bidask())

    assert merged is not None
    # Neue Werte ueberschreiben.
    assert merged.bid == Decimal("271.51")
    assert merged.ask == Decimal("271.59")
    # Alte Werte bleiben erhalten - der Delta-Frame schickte sie nicht mit.
    assert merged.last == Decimal("271.55")
    assert merged.volume == 1234567
    assert merged.high == Decimal("275.00")
    assert merged.availability_code == "DPB"
    # Updated-Marker reflektiert den neuen Frame.
    assert merged.updated_at == 1777415639002


def test_first_frame_carries_all_known_fields_as_snapshot() -> None:
    """Voll-Snapshot beim ersten Frame: keines der Anhang-B-Felder bleibt
    aus, sofern der Wire-Frame sie enthaelt."""
    adapter = SmdTopicAdapter()

    snap = adapter.feed(_smd_first_frame())

    assert snap is not None
    populated = {
        name: getattr(snap, name)
        for name in (
            "last",
            "bid",
            "ask",
            "bid_size",
            "ask_size",
            "volume",
            "last_size",
            "change_pct",
            "availability_code",
            "high",
            "low",
            "server_id",
        )
    }
    assert all(value is not None for value in populated.values()), populated


# ---------------------------------------------------------------------------
# 4. Dedup via (conid, _updated)
# ---------------------------------------------------------------------------


def test_duplicate_conid_and_updated_returns_none() -> None:
    adapter = SmdTopicAdapter()

    first = adapter.feed(_smd_first_frame())
    duplicate = adapter.feed(_smd_first_frame())

    assert first is not None
    assert duplicate is None


def test_same_conid_with_new_updated_is_not_a_duplicate() -> None:
    adapter = SmdTopicAdapter()

    adapter.feed(_smd_first_frame())
    second = adapter.feed(_smd_delta_bidask())

    assert second is not None


# ---------------------------------------------------------------------------
# 5. Smoke: Replay gegen Baseline-Recording (keine smd-Frames erwartet)
# ---------------------------------------------------------------------------


_BASELINE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "recorded"
    / "ws"
    / "spike-baseline.jsonl"
)


def test_replay_baseline_recording_yields_no_smd_frames() -> None:
    """Die Baseline enthaelt nur Lifecycle-Frames (system, act, sts, tic).

    Der Adapter darf gegen einen kompletten Replay keinen einzigen
    SmdFrame produzieren - das ist die wichtige Negativ-Garantie:
    nicht-smd-Frames werden sauber ignoriert.
    """
    assert _BASELINE_PATH.exists(), _BASELINE_PATH

    adapter = SmdTopicAdapter()
    emitted: list[SmdFrame] = []

    with _BASELINE_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            entry = json.loads(line)
            parsed = entry.get("parsed")
            if not isinstance(parsed, dict):
                continue
            result = adapter.feed(parsed)
            if result is not None:
                emitted.append(result)

    assert emitted == []


# ---------------------------------------------------------------------------
# 6. Forward-Compat: unbekannte Field-IDs werden ignoriert
# ---------------------------------------------------------------------------


def test_unknown_field_ids_are_ignored() -> None:
    adapter = SmdTopicAdapter()

    frame_with_garbage = {
        "topic": f"smd+{CONID_AAPL}",
        "conid": CONID_AAPL,
        "_updated": 1777415639100,
        "31": "100.00",
        "9999": "garbage",
        "future_only": {"nested": True},
    }
    snap = adapter.feed(frame_with_garbage)

    assert snap is not None
    assert snap.last == Decimal("100.00")


def test_non_smd_topic_returns_none() -> None:
    adapter = SmdTopicAdapter()

    assert adapter.feed({"topic": "sor", "args": {"orderId": 1}}) is None
    assert adapter.feed({"topic": "sts", "args": {}}) is None
    assert adapter.feed({}) is None


def test_conid_extracted_from_topic_suffix_when_field_missing() -> None:
    """Falls der Frame nur ``topic=smd+<conid>`` (ohne separates ``conid``-
    Feld) liefert, leitet der Adapter die conid aus dem Topic ab."""
    adapter = SmdTopicAdapter()

    frame = adapter.feed(
        {
            "topic": f"smd+{CONID_AAPL}",
            "_updated": 5,
            "31": "200.00",
        }
    )
    assert frame is not None
    assert frame.conid == CONID_AAPL


def test_decimal_string_with_close_prefix_is_handled() -> None:
    """IBKR praefixt unveraenderten Last-Preis manchmal mit ``C`` (close)."""
    adapter = SmdTopicAdapter()

    frame = adapter.feed(
        {
            "topic": f"smd+{CONID_AAPL}",
            "conid": CONID_AAPL,
            "_updated": 1,
            "31": "C271.55",
        }
    )
    assert frame is not None
    assert frame.last == Decimal("271.55")


def test_two_conids_keep_independent_snapshots() -> None:
    adapter = SmdTopicAdapter()

    msft_first = {
        "topic": "smd+272093",
        "conid": 272093,
        "_updated": 1,
        "31": "320.00",
    }
    aapl_first = _smd_first_frame()

    a = adapter.feed(aapl_first)
    m = adapter.feed(msft_first)

    assert a is not None and m is not None
    assert a.conid == CONID_AAPL
    assert a.last == Decimal("271.55")
    assert m.conid == 272093
    assert m.last == Decimal("320.00")
    # Dedup trifft pro conid, nicht global.
    assert adapter.feed(aapl_first) is None
    again_msft = adapter.feed(
        {**msft_first, "_updated": 2, "84": "319.99"}
    )
    assert again_msft is not None
    assert again_msft.bid == Decimal("319.99")
    assert again_msft.last == Decimal("320.00")  # Snapshot-Erhalt


# ---------------------------------------------------------------------------
# AP-11 K5: Tradeability-Anreicherung im SmdTopicAdapter
# ---------------------------------------------------------------------------


async def test_adapter_with_calendar_service_enriches_tradeability_fields() -> None:
    """End-to-End: Adapter mit Fake-CalendarService ergaenzt
    is_tradeable_now / current_session / exchange_id pro Frame."""
    from datetime import datetime, time, timezone  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    from broker_gateway.cp.calendar import (  # noqa: PLC0415
        CalendarDay,
        CalendarSession,
        ExchangeCalendar,
    )

    tz = ZoneInfo("America/New_York")
    today = datetime(2026, 5, 1, tzinfo=tz).date()
    sample = ExchangeCalendar(
        exchange_id="NASDAQ",
        time_zone="America/New_York",
        days=[
            CalendarDay(
                date=today,
                is_holiday=False,
                sessions=[
                    CalendarSession(
                        type="rth",
                        opens_at=datetime.combine(
                            today, time(9, 30), tzinfo=tz
                        ),
                        closes_at=datetime.combine(
                            today, time(16, 0), tzinfo=tz
                        ),
                    ),
                ],
            )
        ],
    )

    class _FakeCalendarService:
        def __init__(self, calendar):
            self._calendar = calendar
            self.calls: list[str] = []

        async def get(self, exchange_id: str):
            self.calls.append(exchange_id)
            return self._calendar

    fake_service = _FakeCalendarService(sample)

    async def _conid_to_exchange(conid: int) -> str | None:
        return "NASDAQ" if conid == CONID_AAPL else None

    fixed_now = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
    # 14:00 UTC = 10:00 EDT -> mitten in RTH.

    adapter = SmdTopicAdapter(
        calendar_service=fake_service,  # type: ignore[arg-type]
        conid_to_exchange=_conid_to_exchange,
        clock=lambda: fixed_now,
    )

    await adapter.preload_for_conid(CONID_AAPL)
    frame = adapter.feed(_smd_first_frame())

    assert frame is not None
    assert frame.exchange_id == "NASDAQ"
    assert frame.is_tradeable_now is True
    assert frame.current_session == "rth"
    # Service wurde genau einmal befragt - der zweite Frame nutzt den Cache.
    assert fake_service.calls == ["NASDAQ"]


async def test_adapter_without_calendar_service_keeps_fields_none() -> None:
    """Backwards-Compat: ohne CalendarService bleiben Tradeability-
    Felder ``None`` (K1-Verhalten)."""
    adapter = SmdTopicAdapter()
    frame = adapter.feed(_smd_first_frame())
    assert frame is not None
    assert frame.is_tradeable_now is None
    assert frame.current_session is None
    assert frame.exchange_id is None


async def test_preload_with_unknown_conid_leaves_tradeability_none() -> None:
    """Wenn der conid_to_exchange-Lookup ``None`` liefert, bleibt der
    Frame ohne Tradeability-Anreicherung."""

    class _FakeCalendarService:
        async def get(self, exchange_id: str):  # pragma: no cover
            raise AssertionError("nicht aufrufen wenn exchange_id fehlt")

    async def _none_lookup(conid: int) -> str | None:
        return None

    adapter = SmdTopicAdapter(
        calendar_service=_FakeCalendarService(),  # type: ignore[arg-type]
        conid_to_exchange=_none_lookup,
    )
    await adapter.preload_for_conid(CONID_AAPL)
    frame = adapter.feed(_smd_first_frame())

    assert frame is not None
    assert frame.is_tradeable_now is None
    assert frame.current_session is None
    assert frame.exchange_id is None
