# src/utils/logger.py
import logging
import sys
from logging.handlers import RotatingFileHandler


def setup_logger():
    """
    Sets up a Unicode-safe logger based on Config settings.
    Reads LOG_LEVEL directly from settings.py -- no YAML file needed.
    """
    from config.settings import Config

    level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)
    log_file = str(Config.LOGS_DIR / "citeprism.log")

    root = logging.getLogger()
    root.setLevel(level)

    # Clear any handlers added before this runs (e.g. by Streamlit)
    root.handlers.clear()

    formatter = logging.Formatter(Config.LOG_FORMAT)

    # Console handler (UTF-8 safe)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    try:
        console_handler.stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root.addHandler(console_handler)

    # File handler (UTF-8 safe, rotating)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
        errors="replace",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    return root


logger = setup_logger()