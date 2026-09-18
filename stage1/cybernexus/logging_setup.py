"""Uniform logging setup, safe to call from every process."""
from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)-5s [%(processName)s] %(name)s: %(message)s"


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=(level or os.getenv("CN_LOG_LEVEL", "INFO")).upper(),
        format=_FORMAT,
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("scapy").setLevel(logging.ERROR)
