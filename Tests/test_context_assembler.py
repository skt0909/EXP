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
            json={"user_id": 1, "season": "8888-00", "gameweek": 4, "message": "test"},
        )

    assert resp.status_code == 200
    assert resp.json()["response"] == main.CHAT_AVAILABILITY_MESSAGE
    mock_groq.assert_not_called()


def test_no_squad_returns_message_and_never_calls_groq():
    with patch("main.call_groq") as mock_groq:
        resp = client.post(
            "/chat",
            json={"user_id": 8675309, "season": REAL_SEASON, "gameweek": REAL_GAMEWEEK, "message": "test"},
        )

    assert resp.status_code == 200
    assert resp.json()["response"] == "Please select your squad for this gameweek first."
    mock_groq.assert_not_called()


def test_real_squad_prompt_never_contains_raw_predicted_points(engine, real_squad_user):
    captured = {}

    def fake_call_groq(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return "mocked advice"

    with patch("main.call_groq", side_effect=fake_call_groq) as mock_groq:
        resp = client.post(
            "/chat",
            json={
                "user_id": real_squad_user,
                "season": REAL_SEASON,
                "gameweek": REAL_GAMEWEEK,
                "message": "Should I captain Salah instead of Haaland?",
            },
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


def test_groq_failure_returns_fallback_not_500(real_squad_user):
    with patch("main.call_groq", side_effect=main.GroqError("mocked failure")):
        resp = client.post(
            "/chat",
            json={"user_id": real_squad_user, "season": REAL_SEASON, "gameweek": REAL_GAMEWEEK, "message": "test"},
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
        json={"user_id": 1, "season": REAL_SEASON, "gameweek": REAL_GAMEWEEK, "message": "test"},
    )

    assert resp.status_code == 200
    assert resp.json()["response"] == main.DB_UNAVAILABLE_MESSAGE
