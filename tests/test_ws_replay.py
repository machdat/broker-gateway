"""Tests fuer tests/cp_mock/ws_replay.py.

Kanonische Fixture: tests/fixtures/recorded/ws/spike-baseline.jsonl
(bereinigt aus dem K1-Live-Mitschnitt).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.cp_mock.ws_replay import (
    WSFrame,
    WSReplayError,
    iter_client_frames,
    iter_server_frames,
    load_ws_frames,
)


BASELINE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "recorded"
    / "ws"
    / "spike-baseline.jsonl"
)


@pytest.fixture
def baseline_frames() -> list[WSFrame]:
    return load_ws_frames(BASELINE)


def test_baseline_loads_all_frames(baseline_frames: list[WSFrame]) -> None:
    assert len(baseline_frames) == 23
    assert baseline_frames[0].dir == "meta"
    assert baseline_frames[0].topic == "connect"
    assert baseline_frames[1].dir == "out"
    assert baseline_frames[1].topic == "auth"


def test_load_validates_required_keys(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps({"ts": "2026-04-29T00:00:00+00:00", "dir": "in", "topic": "x"})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(WSReplayError, match="'raw'"):
        load_ws_frames(bad)


def test_load_rejects_invalid_dir(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps(
            {
                "ts": "2026-04-29T00:00:00+00:00",
                "dir": "down",
                "topic": "x",
                "raw": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(WSReplayError, match="'dir'='down'"):
        load_ws_frames(bad)


def test_load_skips_blank_lines(tmp_path: Path) -> None:
    fixture = tmp_path / "ok.jsonl"
    fixture.write_text(
        "\n"
        + json.dumps(
            {
                "ts": "2026-04-29T00:00:00+00:00",
                "dir": "out",
                "topic": "tic",
                "raw": "tic",
            }
        )
        + "\n\n",
        encoding="utf-8",
    )
    frames = load_ws_frames(fixture)
    assert len(frames) == 1
    assert frames[0].topic == "tic"


def test_load_keeps_extras_for_backwards_compat(tmp_path: Path) -> None:
    fixture = tmp_path / "ext.jsonl"
    fixture.write_text(
        json.dumps(
            {
                "ts": "2026-04-29T00:00:00+00:00",
                "dir": "in",
                "topic": "sts",
                "raw": "{}",
                "parsed": {},
                "session_id": "abc",
                "future_field": 42,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    frames = load_ws_frames(fixture)
    assert frames[0].extras == {"session_id": "abc", "future_field": 42}


def test_iter_server_frames_only_in(baseline_frames: list[WSFrame]) -> None:
    server = list(iter_server_frames(baseline_frames))
    assert all(f.dir == "in" for f in server)
    topics = [f.topic for f in server]
    assert topics[:3] == ["system", "act", "sts"]
    assert topics.count("tic") == 8  # 2 Client-Pings * 4 Server-Antworten


def test_iter_client_frames_only_out(baseline_frames: list[WSFrame]) -> None:
    client = list(iter_client_frames(baseline_frames))
    assert all(f.dir == "out" for f in client)
    assert [f.topic for f in client] == ["auth", "tic", "tic"]


def test_iter_preserves_order(baseline_frames: list[WSFrame]) -> None:
    server = list(iter_server_frames(baseline_frames))
    timestamps = [f.ts for f in server]
    assert timestamps == sorted(timestamps)


def test_empty_body_supported() -> None:
    frame = WSFrame(
        ts=datetime(2026, 4, 29, tzinfo=timezone.utc),
        dir="out",
        topic="tic",
        raw="tic",
        parsed=None,
    )
    assert frame.body == "tic"


def test_parsed_takes_precedence_over_raw() -> None:
    frame = WSFrame(
        ts=datetime(2026, 4, 29, tzinfo=timezone.utc),
        dir="in",
        topic="sts",
        raw='{"topic":"sts"}',
        parsed={"topic": "sts"},
    )
    assert frame.body == {"topic": "sts"}


def test_inter_frame_delay_compressed() -> None:
    base = datetime(2026, 4, 29, tzinfo=timezone.utc)
    frames = [
        WSFrame(ts=base, dir="in", topic="a", raw=""),
        WSFrame(ts=base + timedelta(seconds=10), dir="in", topic="b", raw=""),
        WSFrame(ts=base + timedelta(seconds=15), dir="in", topic="c", raw=""),
    ]
    sleeps: list[float] = []
    out = list(
        iter_server_frames(
            frames,
            timing="compressed",
            compression_factor=0.1,
            sleep=sleeps.append,
        )
    )
    assert [f.topic for f in out] == ["a", "b", "c"]
    assert sleeps == pytest.approx([1.0, 0.5])


def test_default_timing_is_zero_sleep() -> None:
    base = datetime(2026, 4, 29, tzinfo=timezone.utc)
    frames = [
        WSFrame(ts=base, dir="in", topic="a", raw=""),
        WSFrame(ts=base + timedelta(seconds=5), dir="in", topic="b", raw=""),
    ]
    sleeps: list[float] = []
    list(iter_server_frames(frames, sleep=sleeps.append))
    assert sleeps == []
