"""Strukturiertes Logging mit Multi-Strang-Routing.

Pipeline-Harmonisierung: sowohl ``structlog.get_logger()``-Bound-Logger
als auch ``logging.getLogger()``-stdlib-Logger laufen durch denselben
JSONRenderer. Damit ist die README-Aussage "jede Log-Zeile ist ein
JSON-Dict" tatsaechlich wahr - auch fuer Module wie throttle, streams,
cp.lifecycle, die ueber stdlib loggen.

Routing per Logger-Name + ``propagate=False``:

* ``broker_gateway.http``    -> ``inbound.log``  (Observability-Middleware)
* ``broker_gateway.cp.wire`` -> ``cp_wire.log``  (kommender CP-Wire-Logger)
* ``broker_gateway``         -> ``app.log``      (Lifecycle, Throttle, Streams, ...)

Ohne gesetzte ``BG_LOG_DIR`` schreiben alle drei Strang-Logger weiter
auf stdout (Backwards-Kompatibilitaet zum bisherigen Verhalten).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import structlog


_CONFIGURED = False

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
_DEFAULT_BACKUP_COUNT = 20

_STRAND_LOGGERS: tuple[str, ...] = (
    "broker_gateway.http",
    "broker_gateway.cp.wire",
    "broker_gateway",
)


class _LazyStdout:
    """Schreibt jedes Mal auf das aktuell aktive ``sys.stdout``.

    Wichtig fuer Tests, die ``sys.stdout`` per ``capsys`` patchen, nachdem
    der StreamHandler bereits konstruiert wurde - ohne diesen Wrapper
    haelt der Handler die Reference vom Modul-Import-Zeitpunkt.
    """

    def write(self, msg: str) -> int:
        return sys.stdout.write(msg)

    def flush(self) -> None:
        sys.stdout.flush()


def configure_logging(level: str | None = None) -> None:
    """Konfiguriert structlog + stdlib-Pipeline gemeinsam.

    Idempotent: mehrfacher Aufruf ist no-op (relevant fuer Tests, die
    ``create_app`` mehrfach im selben Prozess hochfahren). Tests, die
    eine geaenderte ENV-Variable wirken sehen wollen, koennen
    :func:`reset_for_testing` rufen.

    ENV-Variablen:

    * ``BG_LOG_DIR`` - leer = stdout (Default), gesetzt = drei Datei-Sinks.
    * ``BG_LOG_LEVEL`` - Default ``INFO``; vom ``level``-Parameter ueberschrieben.
    * ``BG_LOG_ROTATE_MAX_BYTES`` - Default 10 MiB; pro Strang ueberschreibbar
      mit ``BG_LOG_INBOUND_MAX_BYTES``, ``BG_LOG_CP_WIRE_MAX_BYTES``,
      ``BG_LOG_APP_MAX_BYTES``.
    * ``BG_LOG_ROTATE_BACKUP_COUNT`` - Default 20; pro Strang ueberschreibbar
      mit ``BG_LOG_INBOUND_BACKUP_COUNT``, ``BG_LOG_CP_WIRE_BACKUP_COUNT``,
      ``BG_LOG_APP_BACKUP_COUNT``.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    actual_level = (level or os.environ.get("BG_LOG_LEVEL") or "INFO").upper()
    log_level = getattr(logging, actual_level, logging.INFO)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    log_dir_str = (os.environ.get("BG_LOG_DIR") or "").strip()
    log_dir = Path(log_dir_str) if log_dir_str else None

    # Root-Logger neu aufsetzen, damit kein Plain-Text-Handler aus einem
    # frueheren basicConfig-Lauf doppelt schreibt.
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)

    # Strang-Logger zuruecksetzen - vorherige Test-Konfiguration darf
    # nicht nachklingen.
    for name in _STRAND_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.setLevel(log_level)
        lg.propagate = True

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)

        _attach_strand(
            "broker_gateway.http",
            log_dir / "inbound.log",
            "INBOUND",
            formatter,
            log_level,
        )
        _attach_strand(
            "broker_gateway.cp.wire",
            log_dir / "cp_wire.log",
            "CP_WIRE",
            formatter,
            log_level,
        )
        _attach_strand(
            "broker_gateway",
            log_dir / "app.log",
            "APP",
            formatter,
            log_level,
        )
    else:
        stdout_handler = logging.StreamHandler(stream=_LazyStdout())
        stdout_handler.setFormatter(formatter)
        stdout_handler.setLevel(log_level)
        root.addHandler(stdout_handler)

    _CONFIGURED = True


def _attach_strand(
    logger_name: str,
    path: Path,
    env_prefix: str,
    formatter: logging.Formatter,
    log_level: int,
) -> None:
    """Haengt einen RotatingFileHandler an einen Strang-Logger.

    ``propagate=False`` verhindert, dass das Event zusaetzlich am
    Parent-Logger landet - sonst wuerden inbound-/cp_wire-Events auch
    nach app.log fliessen (Cross-Talk).
    """
    handler = logging.handlers.RotatingFileHandler(
        filename=str(path),
        maxBytes=_int_env(
            f"BG_LOG_{env_prefix}_MAX_BYTES",
            _int_env("BG_LOG_ROTATE_MAX_BYTES", _DEFAULT_MAX_BYTES),
        ),
        backupCount=_int_env(
            f"BG_LOG_{env_prefix}_BACKUP_COUNT",
            _int_env("BG_LOG_ROTATE_BACKUP_COUNT", _DEFAULT_BACKUP_COUNT),
        ),
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    handler.setLevel(log_level)

    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(log_level)
    logger.propagate = False


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)


def reset_for_testing() -> None:
    """Macht :func:`configure_logging` wieder rufbar - nur fuer Tests.

    Setzt sowohl das Modul-Flag als auch den structlog-Default-Stand
    zurueck. Vor dem naechsten ``configure_logging``-Aufruf koennen Tests
    so ENV-Variablen via ``monkeypatch`` aendern und die Wirkung
    verifizieren.
    """
    global _CONFIGURED
    _CONFIGURED = False
    structlog.reset_defaults()
    for name in _STRAND_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    root = logging.getLogger()
    root.handlers.clear()


__all__ = ["configure_logging", "get_logger", "reset_for_testing"]
