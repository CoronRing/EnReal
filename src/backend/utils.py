# app/utils.py
import logging
import os
import sys


def configure_logging():
    level_name = os.getenv("ENREAL_LOGLEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Avoid duplicate handlers if reloaded
    if not root.handlers:
        root.addHandler(handler)
    root.setLevel(level)