"""Backend settings, overridable by environment variable for deployment."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The scraper library (core/, scrapers/, export/) sits alongside this file.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _origins() -> list[str]:
    """Browser origins allowed to call this API.

    Defaults cover local development. In production set CORS_ORIGINS to the
    deployed frontend URL, comma separated:

        CORS_ORIGINS=https://scraper.yourdomain.com
    """
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
    ]


CORS_ORIGINS = _origins()

# How many log lines to keep in memory per job.
JOB_LOG_LIMIT = int(os.getenv("JOB_LOG_LIMIT", "500"))

# How many finished jobs to keep in history.
JOB_HISTORY_LIMIT = int(os.getenv("JOB_HISTORY_LIMIT", "25"))

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
