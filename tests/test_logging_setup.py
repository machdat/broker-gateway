"""Tests fuer logging_setup: Multi-Strang-Routing + Pipeline-Harmonisierung."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import structlog

from broker_gateway.logging_setup import (
    configure_logging,
    get_logger,
    reset_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_logging():
    """Stellt sicher, dass jeder Test mit frischer Konfiguration startet."""
    reset_for_testing()
    yield
    reset_for_testing()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text("utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def test_without_log_dir_no_files_created(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BG_LOG_DIR", raising=False)
    configure_logging()
    log = get_logger("broker_gateway.http")
    log.info("http_request", request_id="r1", status=200)
    assert not list(tmp_path.glob("*.log"))


def test_strand_routing_inbound_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()
    log = get_logger("broker_gateway.http")
    log.info("http_request", request_id="r1", status=200)

    inbound = _read_jsonl(tmp_path / "inbound.log")
    cp_wire = _read_jsonl(tmp_path / "cp_wire.log")
    app = _read_jsonl(tmp_path / "app.log")

    assert len(inbound) == 1
    assert inbound[0]["event"] == "http_request"
    assert inbound[0]["request_id"] == "r1"
    assert inbound[0]["status"] == 200
    assert "timestamp" in inbound[0]
    assert inbound[0]["level"] == "info"
    assert cp_wire == [], "cp_wire darf bei propagate=False keinen http-Event sehen"
    assert app == [], "app darf bei propagate=False keinen http-Event sehen"


def test_strand_routing_cp_wire_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()
    log = get_logger("broker_gateway.cp.wire")
    log.info("cp_wire", method="GET", status=200, latency_ms=12.3)

    inbound = _read_jsonl(tmp_path / "inbound.log")
    cp_wire = _read_jsonl(tmp_path / "cp_wire.log")
    app = _read_jsonl(tmp_path / "app.log")

    assert inbound == []
    assert len(cp_wire) == 1
    assert cp_wire[0]["event"] == "cp_wire"
    assert cp_wire[0]["latency_ms"] == 12.3
    assert app == []


def test_strand_routing_app_catches_other_loggers(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()
    log = get_logger("broker_gateway.cp.lifecycle")
    log.info("session_started")

    inbound = _read_jsonl(tmp_path / "inbound.log")
    cp_wire = _read_jsonl(tmp_path / "cp_wire.log")
    app = _read_jsonl(tmp_path / "app.log")

    assert inbound == []
    assert cp_wire == []
    assert len(app) == 1
    assert app[0]["event"] == "session_started"


def test_stdlib_logger_is_harmonized_to_json(
    tmp_path: Path, monkeypatch
) -> None:
    """Pipeline-Harmonisierung: stdlib-Logger landen ebenfalls als JSON."""
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()
    stdlib = logging.getLogger("broker_gateway.throttle.manager")
    stdlib.info("throttle_warmup_done")

    app = _read_jsonl(tmp_path / "app.log")
    assert len(app) == 1
    assert app[0]["event"] == "throttle_warmup_done"
    assert app[0]["level"] == "info"
    assert "timestamp" in app[0]


def test_files_are_created_on_first_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()
    # Erst nach einem write existiert die Datei.
    log = get_logger("broker_gateway.http")
    log.info("http_request")
    assert (tmp_path / "inbound.log").exists()


def test_per_strand_rotate_env_overrides_global(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("BG_LOG_ROTATE_MAX_BYTES", "1024")
    monkeypatch.setenv("BG_LOG_ROTATE_BACKUP_COUNT", "3")
    monkeypatch.setenv("BG_LOG_INBOUND_MAX_BYTES", "2048")
    monkeypatch.setenv("BG_LOG_INBOUND_BACKUP_COUNT", "5")
    configure_logging()

    inbound_handler = next(
        h for h in logging.getLogger("broker_gateway.http").handlers
    )
    cp_wire_handler = next(
        h for h in logging.getLogger("broker_gateway.cp.wire").handlers
    )
    app_handler = next(
        h for h in logging.getLogger("broker_gateway").handlers
    )

    assert inbound_handler.maxBytes == 2048
    assert inbound_handler.backupCount == 5
    assert cp_wire_handler.maxBytes == 1024
    assert cp_wire_handler.backupCount == 3
    assert app_handler.maxBytes == 1024
    assert app_handler.backupCount == 3


def test_configure_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()
    handler_count_first = len(logging.getLogger("broker_gateway.http").handlers)
    configure_logging()
    handler_count_second = len(logging.getLogger("broker_gateway.http").handlers)
    assert handler_count_first == handler_count_second == 1


def test_contextvars_propagate_into_event(
    tmp_path: Path, monkeypatch
) -> None:
    """structlog.contextvars.bind_contextvars muss in cp_wire-Events landen.

    Das ist die Grundlage fuer request_id-Korrelation zwischen inbound und
    cp_wire (siehe Karte 'CP-Wire-Log').
    """
    monkeypatch.setenv("BG_LOG_DIR", str(tmp_path))
    configure_logging()
    structlog.contextvars.bind_contextvars(request_id="abc-123")
    try:
        log = get_logger("broker_gateway.cp.wire")
        log.info("cp_wire", method="GET")
    finally:
        structlog.contextvars.clear_contextvars()

    cp_wire = _read_jsonl(tmp_path / "cp_wire.log")
    assert len(cp_wire) == 1
    assert cp_wire[0]["request_id"] == "abc-123"
