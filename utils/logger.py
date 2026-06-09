"""
logger.py — PolyTrader28 Logging Configuration
================================================
Sets up structured logging to both console and a rotating file.

Log files are stored in:  logs/polytrader.log  (rotated daily)
Console output uses colored levels for readability.

Usage:
    from utils.logger import logger
    logger.info("Bot started")
    logger.error("API error: %s", err)
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


# ---------------------------------------------------------------------------
# Log directory
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "polytrader.log"


# ---------------------------------------------------------------------------
# Custom formatter with optional colour for the console
# ---------------------------------------------------------------------------

class _ColourFormatter(logging.Formatter):
    """Add ANSI colour codes to console log messages based on severity level."""

    # ANSI escape sequences
    _COLOURS = {
        logging.DEBUG:     "\033[36m",      # cyan
        logging.INFO:      "\033[32m",      # green
        logging.WARNING:   "\033[33m",      # yellow
        logging.ERROR:     "\033[31m",      # red
        logging.CRITICAL:  "\033[41m",      # red background
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self._COLOURS.get(record.levelno, self._RESET)
        # Temporarily prefix the level name with colour
        original_levelname = record.levelname
        record.levelname = f"{colour}{record.levelname}{self._RESET}"
        result = super().format(record)
        record.levelname = original_levelname  # restore
        return result


# ---------------------------------------------------------------------------
# Logger initialisation
# ---------------------------------------------------------------------------

def _setup_logger() -> logging.Logger:
    """
    Create and configure the application logger.

    Returns:
        A configured Logger instance.
    """
    logger = logging.getLogger("polytrader")
    logger.setLevel(logging.DEBUG)

    # Avoid adding duplicate handlers if this module is reloaded
    if logger.handlers:
        return logger

    # --- File handler (rotating, detailed) ---------------------------------
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(threadName)-12s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    # --- Console handler (coloured, INFO+) ----------------------------------
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = (
        "%(asctime)s | %(levelname)-8s | %(message)s"
    )
    console_handler.setFormatter(_ColourFormatter(console_format, datefmt="%H:%M:%S"))
    logger.addHandler(console_handler)

    return logger


# ---------------------------------------------------------------------------
# Singleton instance
# ---------------------------------------------------------------------------
logger: logging.Logger = _setup_logger()
"""
Project-wide logger.  Import with: from utils.logger import logger

Usage:
    logger.info("Processing opportunity: %s", market_name)
    logger.warning("Edge below threshold: %.2f%%", edge)
    logger.error("API request failed: %s", exc_info=True)
"""
