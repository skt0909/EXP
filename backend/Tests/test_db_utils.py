"""
test_db_utils.py — unit tests for Shared/db_utils.py's URL-resolution
(_database_url), engine caching (get_engine), and credential-stripping
(safe_url).

This was five near-identical files (Game_logic, Worker, Predict,
Feature_engineering, Data/utils) and is now one, so this file
no longer covers "the shared pattern" by proxy -- it covers the single
implementation every stage imports, directly.

Note these tests monkeypatch db_utils.load_dotenv by attribute, which
only works against the module that actually defines _database_url. That
is why the consolidation deleted the old copies rather than leaving
re-export shims behind: patching a shim's namespace would not have
reached the real function, and this file would have started passing for
the wrong reason.
"""

import pytest

from Shared import db_utils


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    """get_engine() is @lru_cache'd -- clear it before and after every test
    so one test's monkeypatched URL can't leak a cached Engine into the next."""
    db_utils.get_engine.cache_clear()
    yield
    db_utils.get_engine.cache_clear()


def test_database_url_prefers_test_database_url_when_both_set(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@host/test_db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/dev_db")

    assert db_utils._database_url() == "postgresql://u:p@host/test_db"


def test_database_url_falls_back_to_database_url_when_test_unset(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/dev_db")

    assert db_utils._database_url() == "postgresql://u:p@host/dev_db"


def test_database_url_raises_when_neither_set(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Prevent the real project .env from silently repopulating DATABASE_URL.
    monkeypatch.setattr(db_utils, "load_dotenv", lambda *a, **kw: None)

    with pytest.raises(RuntimeError, match="DATABASE_URL not set"):
        db_utils._database_url()


def test_safe_url_strips_credentials(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://myuser:supersecret@localhost:5432/fpl_game_test")

    result = db_utils.safe_url()

    assert "myuser" not in result
    assert "supersecret" not in result
    assert result == "localhost:5432/fpl_game_test"


def test_get_engine_is_cached_singleton(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@host/test_db")

    first = db_utils.get_engine()
    second = db_utils.get_engine()

    assert first is second


def test_get_engine_reflects_url_at_first_call(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@host/test_db")

    engine = db_utils.get_engine()

    assert engine.url.database == "test_db"
    assert engine.url.host == "host"
