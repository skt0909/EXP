"""
db_utils.py — shared database helper for Game_logic.

get_engine() reads DATABASE_URL from the project .env and returns a
SQLAlchemy Engine. Mirrors Data_ingestion/utils/db_utils.py,
Feature_engineering/db_utils.py, and Predict/db_utils.py so those stages
each keep their own local copy -- but this is now also the single
canonical db_utils for Context_assembler, imported explicitly via
`from Game_logic.db_utils import get_engine` (Context_assembler no
longer has its own db_utils.py). Both Context_assembler/main.py and
this package's own squad_selection.py/starting_xi.py import this exact
module -- there's only one physical file backing get_engine() for all
three, no import-order ambiguity.
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
