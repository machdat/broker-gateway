"""Structured-Logging-Konfiguration via structlog.

Single Source of Truth fuer das Log-Format: jede Log-Zeile ist eine
JSON-Dict-Zeile mit Pflichtfeldern (siehe README-Section "Observability").
Bewusst KEIN Token-Wert im Log - die Observability-Middleware schreibt
ausschliesslich `caller_id` und `scopes`.
"""
from __future__ import annotations

import logging
import sys

import structlog


_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Konfiguriert Standard-Logging + structlog mit JSON-Output.

    Idempotent: mehrfacher Aufruf ist no-op (relevant fuer Tests, die
    eine App mehrfach im selben Prozess hochfahren).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)


__all__ = ["configure_logging", "get_logger"]
