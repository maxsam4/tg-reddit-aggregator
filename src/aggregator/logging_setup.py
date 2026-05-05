"""Structured logging via structlog, with optional file sink."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def configure_logging(level: str = "INFO", log_file: str | None = None) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Telethon and asyncpraw are noisy at INFO; only show warnings unless DEBUG is set.
    if log_level > logging.DEBUG:
        logging.getLogger("telethon").setLevel(logging.WARNING)
        logging.getLogger("asyncpraw").setLevel(logging.WARNING)
        logging.getLogger("asyncprawcore").setLevel(logging.WARNING)
        logging.getLogger("anthropic").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
