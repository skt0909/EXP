"""
test_context_assembler.py — FastAPI TestClient tests for
Context_assembler/main.py: the empty-squad short-circuit, prompt safety
(no raw predicted_points leaking to Groq), and the Groq/DB failure
fallbacks.

Uses the real "2025-26" GW4 data already ingested (and already has
ml.ml_predictions rows from Worker.tasks.run_ml_pipeline earlier in this
project) since /chat's chat-availability gate requires real GW1 data to
pass -- building a whole parallel fake season through the public schema
just for this would duplicate a lot of ingestion machinery for no benefit.
A dedicated fake test user (created + torn down per test, cascades on
delete) supplies the squad/selection rows -- doesn't touch or depend on
any real user.
"""

import re
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from conftest import bearer_headers
import main
from main import app

client = TestClient(app)

REAL_SEASON = "2025-26"
REAL_GAMEWEEK = 4

# Real fpl_ids already ingested, with real ml.ml_predictions rows for
# REAL_SEASON/REAL_GAMEWEEK/main.MODEL_VERSION.
SQUAD_FPL_IDS = [1, 5, 6, 256, 8, 237, 21, 381, 449, 430, 249]
CAPTAIN_FPL_ID = 430  # Haaland
VICE_CAPTAIN_FPL_ID = 381  # Salah


@pytest.fixture(scope="session")
def _real_season_seeded(engine):
    """Whether REAL_SEASON/GW1 data is actually present in whatever DB
    this suite is running against. True against the dev DB (real FPL data
    ingested there), False against a freshly migrated/empty test DB --
    in which case the tests depending on it skip with a clear reason
    instead of failing on unrelated-looking assertions."""
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM ml.player_gw_stats WHERE season = :s AND gameweek = 1"),
            {"s": REAL_SEASON},
        ).scalar()
    return count > 0


@pytest.fixture
def real_squad_user(engine):
    with engine.begin() as conn:
        user_id = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
            {"e": "pytest_test_user@example.com", "u": "pytest_test_user", "p": "not_a_real_hash"},
        ).scalar()

        gw_selection_id = conn.execute(
            text(
                "INSERT INTO gw_selections (user_id, season, gameweek, captain_id, vice_captain_id, is_locked) "
                "VALUES (:uid, :s, :gw, :cap, :vc, false) RETURNING id"
            ),
            {"uid": user_id, "s": REAL_SEASON, "gw": REAL_GAMEWEEK, "cap": CAPTAIN_FPL_ID, "vc": VICE_CAPTAIN_FPL_ID},
        ).scalar()

        for i, fpl_id in enumerate(SQUAD_FPL_IDS, start=1):
            conn.execute(
                text(
                    "INSERT INTO starting_xi (gw_selection_id, player_id, position_slot, is_captain, is_vice_captain) "
                    "VALUES (:gsid, :pid, :slot, :cap, :vc)"
                ),
                {
                    "gsid": gw_selection_id,
                    "pid": fpl_id,
                    "slot": i,
                    "cap": fpl_id == CAPTAIN_FPL_ID,
                    "vc": fpl_id == VICE_CAPTAIN_FPL_ID,
                },
            )

    yield user_id

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})  # cascades


@pytest.fixture
def real_user_no_squad(engine):
    """A real, authenticatable user with no gw_selections/starting_xi row at
    all -- get_current_user re-reads the users table on every request (see
    Data/auth.py), so a bearer token naming a made-up id 401s before this
    endpoint's own squad check is ever reached. That was silently masking
    this test: it always ran against a hardcoded fake id, which only ever
    got exercised when _real_season_seeded happened to be True, and 401'd
    every time it was. A real inserted-then-deleted user (same pattern as
    real_squad_user, just without the gw_selections/starting_xi rows) is
    the minimal fix -- authenticates for real, and still has no squad."""
    with engine.begin() as conn:
        user_id = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
            {"e": "pytest_no_squad_user@example.com", "u": "pytest_no_squad_user", "p": "not_a_real_hash"},
        ).scalar()

    yield user_id

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})


def test_no_gw1_data_returns_availability_message_and_never_calls_groq():
    """
    The GW1 chat-availability gate must fire before anything else --
    a season with zero ml.player_gw_stats rows for gameweek=1 should
    short-circuit with the availability message, not reach the squad
    lookup or Groq at all. "8888-00" is a dedicated season string never
    touched by any other test/fixture (confirmed zero rows before
    writing this test), so this doesn't depend on TEST_SEASON cleanup
    timing from other test files.
    """
    with patch("main.call_groq") as mock_groq:
        resp = client.post(
            "/chat",
            json={"season": "8888-00", "gameweek": 4, "message": "test"}, headers=bearer_headers(1)
        )

    assert resp.status_code == 200
    assert resp.json()["response"] == main.CHAT_AVAILABILITY_MESSAGE
    mock_groq.assert_not_called()


def test_no_squad_returns_message_and_never_calls_groq(_real_season_seeded, real_user_no_squad):
    if not _real_season_seeded:
        pytest.skip(f"requires real {REAL_SEASON} GW1 data seeded in the target DB (dev DB only, not a fresh test DB)")

    with patch("main.call_groq") as mock_groq:
        resp = client.post(
            "/chat",
            json={"season": REAL_SEASON, "gameweek": REAL_GAMEWEEK, "message": "test"},
            headers=bearer_headers(real_user_no_squad),
        )

    assert resp.status_code == 200
    assert resp.json()["response"] == main.NO_SQUAD_MESSAGE
    mock_groq.assert_not_called()


def test_real_squad_prompt_never_contains_raw_predicted_points(engine, real_squad_user, _real_season_seeded):
    if not _real_season_seeded:
        pytest.skip(f"requires real {REAL_SEASON} GW1 data seeded in the target DB (dev DB only, not a fresh test DB)")

    captured = {}

    def fake_call_groq(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return "mocked advice"

    with patch("main.call_groq", side_effect=fake_call_groq) as mock_groq:
        resp = client.post(
            "/chat",
            json={
                "season": REAL_SEASON,
                "gameweek": REAL_GAMEWEEK,
                "message": "Should I captain Salah instead of Haaland?",
            }, headers=bearer_headers(real_squad_user)
        )

    assert resp.status_code == 200
    assert resp.json()["response"] == "mocked advice"
    mock_groq.assert_called_once()

    prompt = captured["prompt"]

    internal_ids = pd.read_sql(
        text("SELECT id FROM ml.players WHERE season = :s AND fpl_id = ANY(:ids)"),
        engine,
        params={"s": REAL_SEASON, "ids": SQUAD_FPL_IDS},
    )["id"].tolist()

    real_points = pd.read_sql(
        text(
            "SELECT predicted_points FROM ml.ml_predictions WHERE season = :s AND gameweek = :gw "
            "AND player_id = ANY(:ids) AND model_version = :mv"
        ),
        engine,
        params={"s": REAL_SEASON, "gw": REAL_GAMEWEEK, "ids": internal_ids, "mv": main.MODEL_VERSION},
    )["predicted_points"].tolist()

    assert len(real_points) > 0, "test setup problem -- no real predicted_points found to check against"
    for pp in real_points:
        assert str(round(float(pp), 2)) not in prompt

    # price_current is always X.Y (one decimal, from value/10). predicted_points
    # values have far more precision. Any 2+ decimal-digit number in the
    # prompt would indicate a leaked raw prediction.
    assert not re.search(r"\d+\.\d{2,}", prompt), f"prompt appears to contain a raw numeric value:\n{prompt}"


def test_groq_failure_returns_fallback_not_500(real_squad_user, _real_season_seeded):
    if not _real_season_seeded:
        pytest.skip(f"requires real {REAL_SEASON} GW1 data seeded in the target DB (dev DB only, not a fresh test DB)")

    with patch("main.call_groq", side_effect=main.GroqError("mocked failure")):
        resp = client.post(
            "/chat",
            json={"season": REAL_SEASON, "gameweek": REAL_GAMEWEEK, "message": "test"}, headers=bearer_headers(real_squad_user)
        )

    assert resp.status_code == 200
    assert resp.json()["response"] == main.GROQ_UNAVAILABLE_MESSAGE


def test_db_failure_returns_fallback_not_500(monkeypatch):
    class FakeEngine:
        def connect(self):
            raise OperationalError("mocked connect failure", None, Exception("mocked"))

    monkeypatch.setattr(main, "get_engine", lambda: FakeEngine())

    resp = client.post(
        "/chat",
        json={"season": REAL_SEASON, "gameweek": REAL_GAMEWEEK, "message": "test"}, headers=bearer_headers(1)
    )

    assert resp.status_code == 200
    assert resp.json()["response"] == main.DB_UNAVAILABLE_MESSAGE
