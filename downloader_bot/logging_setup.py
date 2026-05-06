"""Centralised logging setup for both the bot and worker entry points.

The bot and worker each construct one named logger at module import time;
both routed through :func:`init_logger` so the handler attachment, level,
and idempotency guard live in one place. Callers may pass a custom
``formatter`` — the bot uses an ANSI-coloured one for terminal output,
the worker uses a plain ``%(name)s %(message)s`` line for container
log aggregators that prefer machine-friendly text.

The module is named ``logging_setup`` rather than ``logging`` to avoid
shadowing the stdlib ``logging`` module on absolute imports inside this
package.
"""

import logging
from typing import ClassVar

from downloader_bot.config import settings


class LoggingFormatter(logging.Formatter):
    """
    Custom logging formatter for discord.py.

    Attributes:
        black (str): black ANSI code.
        red (str): red ANSI code.
        green (str): green ANSI code.
        yellow (str): yellow ANSI code.
        blue (str): blue ANSI code.
        gray (str): gray ANSI code.
        reset (str): reset ANSI code.
        bold (str): bold ANSI code.
        COLORS (Dict[int, str]): Relates logging level to a colour/style.
    """

    # Colors
    black = "\x1b[30m"
    red = "\x1b[31m"
    green = "\x1b[32m"
    yellow = "\x1b[33m"
    blue = "\x1b[34m"
    gray = "\x1b[38m"
    # Styles
    reset = "\x1b[0m"
    bold = "\x1b[1m"

    COLORS: ClassVar[dict[int, str]] = {
        logging.DEBUG: gray + bold,
        logging.INFO: blue + bold,
        logging.WARNING: yellow + bold,
        logging.ERROR: red,
        logging.CRITICAL: red + bold,
    }

    def format(self, record):
        log_color = self.COLORS[record.levelno]
        log_format = "(black){asctime}(reset) (levelcolor){levelname:<8}(reset) (green){name}(reset) {message}"
        log_format = log_format.replace("(black)", self.black + self.bold)
        log_format = log_format.replace("(reset)", self.reset)
        log_format = log_format.replace("(levelcolor)", log_color)
        log_format = log_format.replace("(green)", self.green + self.bold)
        formatter = logging.Formatter(log_format, "%Y-%m-%d %H:%M:%S", style="{")
        return formatter.format(record)


def init_logger(
    name: str,
    *,
    formatter: logging.Formatter | None = None,
) -> logging.Logger:
    """Return a named logger with one StreamHandler attached.

    Idempotent: if the logger already has any handler, it's returned
    as-is. This protects against double-attachment from watchfiles
    reloads in dev and from accidental re-imports in tests.
    """
    logger = logging.getLogger(name)
    logger.setLevel(settings.LOGGING_LEVEL)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter or LoggingFormatter())
        logger.addHandler(handler)
    return logger
