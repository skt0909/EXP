# Migrations

This project has no SQLAlchemy ORM models (every module uses raw SQL /
SQLAlchemy Core), so `alembic revision --autogenerate` has nothing to diff
against and is not part of this workflow. The baseline revision
(`9fb0900550db_baseline_capture_hand_applied_schema.py`) was hand-authored
from a `pg_dump --schema-only` capture of the live, previously hand-applied
database (`baseline_schema.sql`, next to that revision file) rather than
generated.

**Apply migrations to a fresh database**: set `DATABASE_URL` (in `.env` or
the environment) to point at the target database, then run
`alembic upgrade head` from the repo root.

**Add a migration for a future schema change**: run
`alembic revision -m "description"` to create an empty revision, then
hand-write `upgrade()`/`downgrade()` using `op.*` calls (or `op.execute()`
for anything not covered by Alembic's op helpers, such as triggers/trigger
functions) — not `--autogenerate`, for the reason above.

## Migration docstrings name the paths that existed when they were written

Several revisions here point at file paths that no longer resolve — most
often `Game_logic/scheduling.py`, which no longer exists at all, and
`Game_logic/` paths for modules that now live elsewhere. **These are not
mistakes and are deliberately left as written.**

The backend was reorganised in 2026. Everything that had accumulated in one
`Game_logic/` directory was split by responsibility into `Shared/`,
`Data/`, `Gameplay/`, `GameEngine/` and `Results/` (and `Data_ingestion/`
became `Data/`), leaving `Game_logic/` holding only the Dream11 modules.
Nothing about the *schema* changed; only where the Python lives.

A migration's docstring records why a schema change was made and what was
true when it was made. Rewriting one to name a directory layout that did
not exist yet would make it a worse record, not a more accurate one — it
would read as though the author knew about a structure invented later. So
the dated text stays, and this note exists so that hitting one of those
paths leads somewhere instead of nowhere.

**Where things live now:** see the *Where the game code lives* table in the
[root README](../../README.md), which is kept current. This note deliberately
does not list which revisions are affected or map old paths to new ones — such
a list would itself go stale at the next move, which is the problem it would
be trying to solve. The general rule is enough: a `Game_logic/` path in a
migration docstring means "wherever that module lives today", and the README
table says where that is.

One exception worth knowing: `Migrations/env.py` is **not** a dated record —
it is live configuration — so stale references there are ordinary bugs and
should be fixed rather than preserved.
