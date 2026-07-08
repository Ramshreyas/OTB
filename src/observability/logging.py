"""Structured logging with structlog — JSON for prod, console for dev."""

from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging(
    level: str = "INFO",
    json_output: bool = False,
) -> None:
    """Configure structlog for the entire application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        json_output: If True, emit JSON lines (for production/log aggregation).
            If False, emit human-readable colored output.
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso")

    if json_output:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                timestamper,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
    else:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                timestamper,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.dev.ConsoleRenderer(colors=True),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )


# Auto-configure on import if LOG_LEVEL env var is set
_log_level = os.getenv("LOG_LEVEL", "INFO")
_json = os.getenv("LOG_FORMAT", "").lower() == "json"
configure_logging(level=_log_level, json_output=_json)
