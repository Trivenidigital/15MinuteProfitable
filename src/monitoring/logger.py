"""Structured logging setup for the Polymarket trading bot."""

import logging
import sys

import structlog


def setup_logging(log_level: str = "INFO", log_format: str = "json") -> None:
    """Configure structlog globally.

    Args:
        log_level: The minimum log level (e.g. "DEBUG", "INFO", "WARNING").
        log_format: Output format - "json" for JSONRenderer or "console" for
            ConsoleRenderer with colors.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "console":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO),
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """Return a bound logger with the given name.

    Args:
        name: The logger name, typically the module path.

    Returns:
        A structlog BoundLogger instance bound with the provided name.
    """
    return structlog.get_logger(logger_name=name)
