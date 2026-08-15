# Tests

Run with `pytest backend/Tests -v` from the project root (venv activated). Requires a local Postgres reachable via the same `DATABASE_URL` used by the rest of the project (`.env` at the project root) — these tests hit the real database (a dedicated fake season/test user, cleaned up automatically), not mocks.
