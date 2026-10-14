"""Application logging (rotating file, never fatal)."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from src.utils.paths import state_dir
from src.utils.channel import DEFAULT_CHANNEL, STABLE, get_channel

LOGGER_NAME = STABLE.logger_name


def setup_logging(enabled: bool = True, log_dir: Optional[Path] = None, *,
                  channel: str = DEFAULT_CHANNEL) -> logging.Logger:
    logger = logging.getLogger(get_channel(channel).logger_name)
    logger.setLevel(logging.INFO)
    # Keep host/root handlers from forwarding one channel's records elsewhere.
    logger.propagate = False

    if logger.handlers:
        return logger

    handler: logging.Handler = logging.NullHandler()
    if enabled:
        try:
            directory = Path(log_dir) if log_dir else state_dir(channel)
            directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                directory / "monitor.log",
                maxBytes=512 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
        except OSError:
            handler = logging.NullHandler()

    logger.addHandler(handler)
    return logger
