"""Application logging configuration."""

from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    """Configure root logger once using env-driven log level."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"
    )

    if not root_logger.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)
        return

    for handler in root_logger.handlers:
        handler.setFormatter(formatter)
