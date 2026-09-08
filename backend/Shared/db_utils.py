"""
db_utils.py — the one database helper for every stage of this project.

Previously this file existed five times over, in Game_logic/, Worker/,
Predict/, Feature_engineering/ and Data/utils/. All five were
functionally identical -- same .env walk-up, same
TEST_DATABASE_URL-over-DATABASE_URL precedence, same lru_cache, same
pool_pre_ping=True, same credential-stripping safe_url. Only the
docstrings differed, plus one extra ingestion-only helper
(table_counts), which stays behind in Data/utils/db_utils.py
because it is not shared by anything.

WHY backend/Shared/ rather than promoting one of the five in place: the
importers span the whole stack -- Game_logic, Worker, Data,
Predict and Tools all need this. Making any one of them canonical would
create a dependency in the wrong direction (ingestion importing from
gameplay, say), so it lives in a neutral package that depends on
nothing else in the project.

ENGINE CACHING, and what consolidating changed. get_engine() is
lru_cache'd, so a process gets one Engine and therefore one connection
pool per *module*. With five copies, a process that imported two of
them held two independent pools; the Celery worker did exactly that
(Worker/tasks.py's own copy, plus Game_logic's via the gameweek and
scoring functions it calls). It now holds one. That is a real change in
pooling behaviour, and a deliberate one: it halves the worker's
worst-case connection count rather than raising it, the pools were never
configured differently, and the worker runs --pool=solo (one task at a
time), so a single default pool of 5 + 10 overflow is not a new
constraint. Nothing in the codebase depended on the two pools being
distinct.

The cache is keyed on the `echo` argument, so get_engine() and
get_engine(echo=True) are separate entries -- unchanged from before, and
the reason maxsize is 1 rather than None would be wrong here.
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
        load_dotenv()  # fall back to python-dotenv's own search

    url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
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
