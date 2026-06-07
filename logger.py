"""
utils/logger.py
───────────────
Structured logging via structlog.  Every service binds contextual fields
(user_id, order_id, provider, …) for easy log-aggregation and tracing.
"""

import logging
import sys
from typing import Any

import structlog

from config.settings import get_settings

settings = get_settings()


def _configure_logging() -> None:
    log_level = logging.DEBUG if settings.debug else logging.INFO

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    if settings.is_production:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


_configure_logging()


def get_logger(name: str, **initial_ctx: Any) -> structlog.BoundLogger:
    """Return a bound logger with optional initial context fields."""
    logger = structlog.get_logger(name)
    if initial_ctx:
        logger = logger.bind(**initial_ctx)
    return logger
