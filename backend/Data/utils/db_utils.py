"""
db_utils.py — ingestion-only database helper.

The get_engine/safe_url/_database_url implementation that used to live
here (identically to four other copies) now lives in
backend/Shared/db_utils.py. This module keeps only table_counts, which
is genuinely ingestion-specific -- nothing outside Data has
ever used it -- and re-exports the two shared names so that
`from utils.db_utils import get_engine, safe_url` keeps working for
fpl_ingest.py and utils/__init__.py without either of them changing.

The sys.path insert is what makes `Shared` importable when an ingest
script is run directly (`python fpl_ingest.py ...`), where sys.path[0]
is Data/ rather than backend/. It is a no-op under pytest and
under the Celery worker, both of which already put backend/ on the path.
"""

import sys
from pathlib import Path

# utils/ -> Data/ -> backend/
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import text
from sqlalchemy.engine import Engine

from Shared.db_utils import get_engine, safe_url  # re-exported, see module docstring

__all__ = ["get_engine", "safe_url", "table_counts"]


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
