"""
db_utils.py — shared database helper for feature_engineering.

get_engine() reads DATABASE_URL from the project .env and returns a
SQLAlchemy Engine. The .env lives at the project root (one level above
Feature_engineering/), so we walk up from this file to find it rather
than relying on the current working directory. Mirrors
Data_ingestion/utils/db_utils.py so both stages point at the same
Postgres instance without a cross-folder import dependency.
"""

import os
from pathlib import Path
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_SEARCH_DIRS = [Path(__file__).resolve().parent] + list(Path(__file__).resolve().parents)


def _database_url() -> str:
    """Find and load the nearest .env, then return DATABASE_URL."""
    for d in _SEARCH_DIRS:
        candidate = d / ".env"
        if candidate.is_file():
            load_dotenv(candidate)
            break
    else:
        load_dotenv()

    url = os.getenv("DATABASE_URL")
    if not url:
        looked = "\n  ".join(str(d / ".env") for d in _SEARCH_DIRS[:4])
        raise RuntimeError("DATABASE_URL not set. Looked for a .env in:\n  " + looked)
    return url


@lru_cache(maxsize=1)
def get_engine(echo: bool = False) -> Engine:
    """Return a pooled Engine. Cached, so repeated calls reuse one pool."""
    return create_engine(_database_url(), echo=echo, pool_pre_ping=True)


def safe_url() -> str:
    """DATABASE_URL with credentials stripped, for logging."""
    return _database_url().split("@")[-1]
