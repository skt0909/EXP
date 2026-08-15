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
