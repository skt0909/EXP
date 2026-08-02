"""
db_utils.py — shared database helpers.

get_engine() reads DATABASE_URL from the project .env and returns a
SQLAlchemy Engine. The .env lives at the project root (one level above
Data_ingestion/), so we walk up from this file to find it rather than
relying on the current working directory.
"""

import os
from pathlib import Path
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# Data_ingestion/utils/db_utils.py -> parent=utils, then Data_ingestion, then project root
_SEARCH_DIRS = [Path(__file__).resolve().parent] + list(Path(__file__).resolve().parents)


def _database_url() -> str:
    """Find and load the nearest .env, then return DATABASE_URL."""
    for d in _SEARCH_DIRS:
        candidate = d / ".env"
        if candidate.is_file():
            load_dotenv(candidate)
            break
    else:
        load_dotenv()  # fall back to python-dotenv's own search

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


def table_counts(engine: Engine, schema: str = "ml") -> dict:
    """Row count per table in `schema` — used by the ingest scripts to report state."""
    with engine.connect() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :s ORDER BY table_name"
                ),
                {"s": schema},
            )
        ]
        return {
            t: conn.execute(text(f"SELECT count(*) FROM {schema}.{t}")).scalar()
            for t in tables
        }
