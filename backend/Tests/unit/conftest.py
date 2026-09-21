"""conftest.py for the DB-free unit tests.

The parent backend/Tests/conftest.py declares `_clean_ml_test_data` as an
AUTOUSE fixture that opens a connection before and after every test, so any
file under backend/Tests reaches Postgres whether it wants to or not. Phase 2's
engine is pure -- no SQL, no I/O -- and its tests are supposed to prove that,
which they cannot do from inside a subtree that connects anyway.

Same-name override is the mechanism: pytest resolves fixtures nearest-first, so
redefining both names here shadows the parent's for this directory only. The
rest of the suite is untouched.

`engine` is overridden too, and deliberately raises rather than returning None.
If a test in here ever asks for a database, that is a bug in the test -- it
should fail loudly and name the reason, not quietly receive something falsy and
skip half its assertions.
"""
import pytest


@pytest.fixture(autouse=True)
def _clean_ml_test_data():
    """No-op override. Nothing under this directory touches ml.* tables."""
    yield


@pytest.fixture
def engine():
    raise AssertionError(
        "backend/Tests/unit is database-free by design: the Phase 2 scoring "
        "engine is pure and its tests must prove that. A test here asked for "
        "the `engine` fixture, which means it is reaching for Postgres. Move "
        "it to backend/Tests/ instead."
    )
