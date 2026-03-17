"""Loguru configuration for the full-text extraction service.

Call ``setup_logging()`` once at application startup (inside the lifespan
context manager).  All stdlib ``logging`` records emitted by third-party
libraries (Selenium, httpx, trafilatura, …) are forwarded to loguru so that
the entire application produces a single, consistently formatted log stream.
"""
from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    pass


class _InterceptHandler(logging.Handler):
    """Forward stdlib log records into loguru with correct caller context."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno  # type: ignore[assignment]

        # Walk the call stack to find the originating frame outside loguru/logging.
        frame, depth = logging.currentframe(), 0
        while frame:
            if frame.f_code.co_filename not in (logging.__file__, __file__):
                break
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """Configure loguru and intercept all stdlib logging.

    Args:
        level:     Minimum log level (DEBUG / INFO / WARNING / ERROR).
        json_logs: Emit JSON-formatted lines (useful for log-aggregation
                   pipelines).  Defaults to human-readable coloured output.
    """
    # Remove the default loguru sink so we can replace it with our own.
    logger.remove()

    if json_logs:
        logger.add(
            sys.stdout,
            level=level,
            format="{message}",
            serialize=True,
            enqueue=True,
        )
    else:
        logger.add(
            sys.stdout,
            level=level,
            colorize=True,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                "<level>{message}</level>"
            ),
            enqueue=True,  # thread-safe async-safe writing
            backtrace=True,
            diagnose=False,  # disable variable inspection in production
        )

    # Route all stdlib loggers through loguru.
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # Silence overly verbose third-party loggers.
    for noisy in (
        "selenium.webdriver.remote.remote_connection",
        "urllib3.connectionpool",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
