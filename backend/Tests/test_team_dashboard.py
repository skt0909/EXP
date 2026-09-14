"""
test_team_dashboard.py — FastAPI TestClient tests for
Results/team_dashboard.py's GET /team: lineup/bench grouping by
position, captain/vice-captain flags + chip-based multiplier, team_value/
bank from user_squads+squad_players, gw_points/season_total from
gw_scores, the overall_rank/gw_average computed across this app's users,
gameweek deadline resolution, and the has_lineup=False "not set yet" path.

Seeds gw_selections/starting_xi and gw_scores directly via SQL (same
approach Tests/test_scoring.py uses) rather than running
score_gameweek() -- this endpoint only reads gw_scores, it doesn't
compute it, so the test shouldn't depend on scoring.py's logic.
"""

from datetime import datetime, timezone

from sqlalchemy import text
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SEASON, bearer_headers
# Stamped on every gw_scores row so a rules change is legible in the data;
# see Shared/rules.py.
from Shared.rules import CURRENT_RULES_VERSION
from main import app

client = TestClient(app)

GAMEWEEK = 1
# gw_scores has no autouse cleanup in conftest.py (unlike ml.*), and other
# test files reuse TEST_SEASON + small gameweek numbers (1-8) too -- so a
# rank/average test that counts ALL gw_scores rows for a given
# (season, gameweek) needs a gameweek number nothing else plausibly
# touches, or it inherits whatever leaked rows any other suite/run left
# behind. Confirmed hundreds of such leaked rows exist for gameweek 1/2
# in this environment already.
RANK_TEST_GAMEWEEK = 9042  # gameweek is a DB smallint -- stay under 32767


@pytest.fixture
def test_user(engine):
    with engine.begin() as conn:
        uid = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
            {"e": "pytest_dashboard_test_user@example.com", "u": "pytest_dashboard_test_user", "p": "not_a_real_hash"},
        ).scalar()
    yield uid
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM gw_scores WHERE user_id = :uid"), {"uid": uid})
        conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})  # cascades gw_selections/user_squads


def _seed_squad_player(make_team, make_player, id_offset, i, position, cost, club_idx=None):
    club_idx = club_idx if club_idx is not None else id_offset + i
    team_id = make_team(fpl_id=club_idx, name=f"Club{club_idx}", short_name=f"C{club_idx}")
    fpl_id = id_offset + i
    make_player(fpl_id=fpl_id, position=position, team_id=team_id, cost_start=cost)
    return fpl_id


def _seed_full_squad(engine, make_team, make_player, user_id, season, id_offset=9000):
    """15 players (2GK/5DEF/5MID/3FWD, own club each, cost 60 = £6.0m
    each -> team_value £90.0m), inserted into user_squads/squad_players
    directly (bypassing Gameplay/squad_selection.py's endpoint, which
    this test doesn't need). Returns the 15 fpl_ids in that order."""
    positions = ["GK", "GK"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    fpl_ids = [
        _seed_squad_player(make_team, make_player, id_offset, i, pos, cost=60)
        for i, pos in enumerate(positions)
    ]
    with engine.begin() as conn:
        user_squad_id = conn.execute(
            text(
                "INSERT INTO user_squads (user_id, season, budget_remaining, updated_at) "
                "VALUES (:u, :s, :b, now()) RETURNING id"
            ),
            {"u": user_id, "s": season, "b": 1000 - 15 * 60},  # 1000 tenths cap, 15 * 60 spent
        ).scalar()
        for fpl_id in fpl_ids:
            conn.execute(
                text(
                    "INSERT INTO squad_players (user_squad_id, player_id, purchase_price, sell_price, is_active) "
                    "VALUES (:usid, :pid, 60, NULL, TRUE)"
                ),
                {"usid": user_squad_id, "pid": fpl_id},
            )
    return fpl_ids


def _seed_gw_selection(engine, user_id, season, gameweek, xi_ids, bench_ids, captain_id, vice_captain_id, chip_used=None):
    with engine.begin() as conn:
        gw_selection_id = conn.execute(
            text(
                "INSERT INTO gw_selections (user_id, season, gameweek, captain_id, vice_captain_id, chip_used) "
                "VALUES (:u, :s, :gw, :cap, :vc, :chip) RETURNING id"
            ),
            {"u": user_id, "s": season, "gw": gameweek, "cap": captain_id, "vc": vice_captain_id, "chip": chip_used},
        ).scalar()
        for slot, pid in enumerate(xi_ids, start=1):
            conn.execute(
                text(
                    "INSERT INTO starting_xi (gw_selection_id, player_id, position_slot, is_captain, is_vice_captain) "
                    "VALUES (:gsid, :pid, :slot, :cap, :vc)"
                ),
                {"gsid": gw_selection_id, "pid": pid, "slot": slot, "cap": pid == captain_id, "vc": pid == vice_captain_id},
            )
        for slot, pid in enumerate(bench_ids, start=12):
            conn.execute(
                text(
                    "INSERT INTO starting_xi (gw_selection_id, player_id, position_slot, is_captain, is_vice_captain) "
                    "VALUES (:gsid, :pid, :slot, FALSE, FALSE)"
                ),
                {"gsid": gw_selection_id, "pid": pid, "slot": slot},
            )
    return gw_selection_id


def _seed_gw_score(engine, user_id, season, gameweek, total_points, season_total):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gw_scores (user_id, season, gameweek, raw_points, final_points, transfer_hits, "
                "hit_deductions, total_points, season_total, rules_version) "
                "VALUES (:u, :s, :gw, :tp, :tp, 0, 0, :tp, :st, :rv)"
            ),
            {"u": user_id, "s": season, "gw": gameweek, "tp": total_points, "st": season_total,
             "rv": CURRENT_RULES_VERSION},
        )


def test_full_dashboard_happy_path(engine, make_team, make_player, test_user):
    # _seed_full_squad's 15 players are ["GK","GK"] + ["DEF"]*5 + ["MID"]*5 + ["FWD"]*3
    # (indices 0-14); slicing the first 11 as the starting XI (indices 0-10)
    # therefore lands as 2 GK + 5 DEF + 4 MID + 0 FWD, with the 5th MID and
    # all 3 FWDs on the bench (indices 11-14) -- this endpoint has no
    # formation rule of its own (it only displays whatever starting_xi
    # already holds), so the split doesn't need to be a legal 1-GK/3-5-DEF
    # starting XI, just a known, assertable shape.
    fpl_ids = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON)
    xi_ids, bench_ids = fpl_ids[:11], fpl_ids[11:]
    captain, vice = xi_ids[9], xi_ids[10]  # the last two MIDs in the XI
    _seed_gw_selection(engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, captain, vice)
    _seed_gw_score(engine, test_user, TEST_SEASON, GAMEWEEK, total_points=64, season_total=284)

    resp = client.get("/team", params={"season": TEST_SEASON, "gameweek": GAMEWEEK}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_lineup"] is True
    assert body["gw_points"] == 64
    assert body["season_total"] == 284
    assert body["chip_used"] is None
    assert body["captain_multiplier"] == 2
    assert body["team_value"] == 90.0  # 15 * £6.0m
    assert body["bank"] == (1000 - 15 * 60) / 10

    assert len(body["lineup"]["GK"]) == 2
    assert len(body["lineup"]["DEF"]) == 5
    assert len(body["lineup"]["MID"]) == 4
    assert len(body["lineup"]["FWD"]) == 0
    assert len(body["bench"]) == 4

    all_lineup_players = [p for players in body["lineup"].values() for p in players]
    captain_lines = [p for p in all_lineup_players if p["is_captain"]]
    vice_lines = [p for p in all_lineup_players if p["is_vice_captain"]]
    assert len(captain_lines) == 1 and captain_lines[0]["player_id"] == captain
    assert len(vice_lines) == 1 and vice_lines[0]["player_id"] == vice


def test_triple_captain_chip_reports_3x_multiplier(engine, make_team, make_player, test_user):
    fpl_ids = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON)
    xi_ids, bench_ids = fpl_ids[:11], fpl_ids[11:]
    _seed_gw_selection(
        engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, xi_ids[0], xi_ids[1], chip_used="triple_captain"
    )

    resp = client.get("/team", params={"season": TEST_SEASON, "gameweek": GAMEWEEK}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["chip_used"] == "triple_captain"
    assert body["captain_multiplier"] == 3


def test_no_lineup_yet_returns_empty_not_error(test_user):
    resp = client.get("/team", params={"season": TEST_SEASON, "gameweek": GAMEWEEK}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_lineup"] is False
    assert body["lineup"] == {"GK": [], "DEF": [], "MID": [], "FWD": []}
    assert body["bench"] == []
    assert body["gw_points"] == 0
    assert body["season_total"] == 0
    assert body["team_value"] == 0.0
    assert body["bank"] == 0.0


def test_overall_rank_and_gw_average_scoped_to_this_gameweek(engine, make_team, make_player, test_user):
    with engine.begin() as conn:
        other_a = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
            {"e": "pytest_dashboard_other_a@example.com", "u": "pytest_dashboard_other_a", "p": "x"},
        ).scalar()
        other_b = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
            {"e": "pytest_dashboard_other_b@example.com", "u": "pytest_dashboard_other_b", "p": "x"},
        ).scalar()

    try:
        _seed_gw_score(engine, test_user, TEST_SEASON, RANK_TEST_GAMEWEEK, total_points=50, season_total=200)  # rank 2
        _seed_gw_score(engine, other_a, TEST_SEASON, RANK_TEST_GAMEWEEK, total_points=70, season_total=300)  # rank 1
        _seed_gw_score(engine, other_b, TEST_SEASON, RANK_TEST_GAMEWEEK, total_points=30, season_total=100)  # rank 3

        resp = client.get("/team", params={"season": TEST_SEASON, "gameweek": RANK_TEST_GAMEWEEK}, headers=bearer_headers(test_user))

        assert resp.status_code == 200
        body = resp.json()
        assert body["overall_rank"] == 2
        assert body["overall_rank_total"] == 3
        assert body["gw_average"] == 50.0  # (50 + 70 + 30) / 3
    finally:
        with engine.begin() as conn:
            for uid in (other_a, other_b):
                conn.execute(text("DELETE FROM gw_scores WHERE user_id = :uid"), {"uid": uid})
                conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})


def test_no_gw_scores_at_all_gives_null_rank_and_average(test_user):
    resp = client.get("/team", params={"season": TEST_SEASON, "gameweek": RANK_TEST_GAMEWEEK}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_rank"] is None
    assert body["overall_rank_total"] is None
    assert body["gw_average"] is None


def test_deadline_resolved_from_fixtures(engine, make_team, make_fixture, test_user):
    home = make_team(fpl_id=70001, name="Home FC", short_name="HOM")
    away = make_team(fpl_id=70002, name="Away FC", short_name="AWY")
    kickoff = datetime(2026, 1, 2, 15, 0, 0, tzinfo=timezone.utc)
    make_fixture(fpl_id=80001, gameweek=GAMEWEEK, home_team_id=home, away_team_id=away, kickoff_time=kickoff)

    resp = client.get("/team", params={"season": TEST_SEASON, "gameweek": GAMEWEEK}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["deadline"] is not None
    assert body["deadline"].startswith("2026-01-02T13:30:00")


def test_deadline_none_when_fixtures_not_ingested_yet(test_user):
    resp = client.get("/team", params={"season": TEST_SEASON, "gameweek": GAMEWEEK}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    assert resp.json()["deadline"] is None


def test_identity_fields_reflect_username_and_null_team_name(test_user):
    """test_user's fixture never sets team_name -- confirms the column
    comes back None rather than erroring when it's just never been set,
    same as this endpoint's other "not configured yet" fields."""
    resp = client.get("/team", params={"season": TEST_SEASON, "gameweek": GAMEWEEK}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == test_user
    assert body["username"] == "pytest_dashboard_test_user"
    assert body["team_name"] is None


def test_identity_fields_reflect_team_name_once_set(engine, test_user):
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET team_name = :t WHERE id = :uid"), {"t": "Test FC", "uid": test_user})

    resp = client.get("/team", params={"season": TEST_SEASON, "gameweek": GAMEWEEK}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    assert resp.json()["team_name"] == "Test FC"


def test_a_token_naming_a_nonexistent_user_is_rejected_not_served():
    """Replaces an older test that asserted a nonexistent user_id came back
    as a 200 with null username/team_name.

    That behaviour is gone, and deliberately: identity is no longer a
    parameter anyone can invent. get_current_user re-reads the users row on
    every request, so a token naming a user who does not exist -- or who has
    since been deleted -- is a 401 on the credential rather than a cheerful
    empty dashboard. The old shape was harmless only because the id it
    echoed was already untrusted.
    """
    resp = client.get("/team", params={"season": TEST_SEASON, "gameweek": GAMEWEEK},
                      headers=bearer_headers(99999999))

    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid token"


# --------------------------------------------------------- points breakdown


def _seed_gw_score_breakdown(engine, user_id, season, gameweek, *, raw, final, hits, deductions):
    """Unlike _seed_gw_score above (which collapses everything into one
    number), this writes a breakdown whose parts all differ -- so a handler
    that confused raw_points with total_points fails here instead of
    coincidentally passing."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gw_scores (user_id, season, gameweek, raw_points, final_points, "
                "transfer_hits, hit_deductions, total_points, season_total, rules_version) "
                "VALUES (:u, :s, :gw, :raw, :final, :hits, :ded, :total, :total, :rv)"
            ),
            {
                "u": user_id, "s": season, "gw": gameweek, "raw": raw, "final": final,
                "hits": hits, "ded": deductions, "total": final - deductions,
                "rv": CURRENT_RULES_VERSION,
            },
        )


def test_points_breakdown_does_not_double_count_captain(engine, test_user):
    """The exact bug these fields exist to prevent: raw_points is the XI at
    TRUE base value with the captain UN-doubled, and captain_bonus is the
    captain's extra ONLY. Mirrors the design's worked example: 52 + 16 - 4 = 64."""
    gw = 9101
    _seed_gw_score_breakdown(engine, test_user, TEST_SEASON, gw, raw=52, final=68, hits=1, deductions=4)

    body = client.get(
        "/team", params={"season": TEST_SEASON, "gameweek": gw}, headers=bearer_headers(test_user)
    ).json()

    assert body["has_score"] is True
    assert body["raw_points"] == 52
    assert body["captain_bonus"] == 16           # 68 - 52: the extra, not the whole captain
    assert body["transfer_hits"] == 1
    assert body["hit_deductions"] == 4
    assert body["final_total"] == 64             # 52 + 16 - 4
    # The identity the Dashboard table renders must actually hold.
    assert body["raw_points"] + body["captain_bonus"] - body["hit_deductions"] == body["final_total"]
    # A double-counted captain would surface as raw == final (68) and a total of 80.
    assert body["raw_points"] != body["final_total"]


def test_breakdown_zeroed_and_flagged_when_not_scored_yet(test_user):
    """No gw_scores row: every number is 0, but has_score is False so the UI can
    say "not scored yet" rather than render a real-looking zero."""
    body = client.get(
        "/team", params={"season": TEST_SEASON, "gameweek": 9102}, headers=bearer_headers(test_user)
    ).json()

    assert body["has_score"] is False
    assert body["raw_points"] == 0
    assert body["captain_bonus"] == 0
    assert body["final_total"] == 0


def test_triple_captain_bonus_is_twice_the_base(engine, test_user):
    """Under triple_captain the multiplier is 3, so a 10-point captain adds
    (3-1)*10 = 20 of bonus, not 10."""
    gw = 9103
    _seed_gw_score_breakdown(engine, test_user, TEST_SEASON, gw, raw=40, final=60, hits=0, deductions=0)

    body = client.get(
        "/team", params={"season": TEST_SEASON, "gameweek": gw}, headers=bearer_headers(test_user)
    ).json()
    assert body["captain_bonus"] == 20
    assert body["final_total"] == 60


# ------------------------------------------------------------- live status


def test_live_status_upcoming_when_no_fixtures_ingested(test_user):
    body = client.get(
        "/team", params={"season": TEST_SEASON, "gameweek": 9104}, headers=bearer_headers(test_user)
    ).json()
    assert body["live_status"] == "upcoming"


def test_live_status_is_live_until_every_fixture_is_finished(
    engine, make_team, make_fixture, test_user
):
    gw = 9105
    home = make_team(fpl_id=9701, name="HomeFC", short_name="HOM")
    away = make_team(fpl_id=9702, name="AwayFC", short_name="AWY")
    kicked_off = datetime(2020, 1, 1, tzinfo=timezone.utc)
    make_fixture(fpl_id=9801, gameweek=gw, home_team_id=home, away_team_id=away,
                 kickoff_time=kicked_off, finished=True)
    unfinished = make_fixture(fpl_id=9802, gameweek=gw, home_team_id=away, away_team_id=home,
                              kickoff_time=kicked_off, finished=False)

    # One kicked-off-but-unfinished fixture keeps the gameweek live, so partial
    # points are never presented as a settled score.
    body = client.get("/team", params={"season": TEST_SEASON, "gameweek": gw}, headers=bearer_headers(test_user)).json()
    assert body["live_status"] == "live"

    with engine.begin() as conn:
        conn.execute(text("UPDATE ml.fixtures SET finished = TRUE WHERE id = :i"), {"i": unfinished})

    body = client.get("/team", params={"season": TEST_SEASON, "gameweek": gw}, headers=bearer_headers(test_user)).json()
    assert body["live_status"] == "final"


def test_live_status_upcoming_when_fixtures_have_not_kicked_off(make_team, make_fixture, test_user):
    gw = 9106
    home = make_team(fpl_id=9703, name="H2", short_name="H2")
    away = make_team(fpl_id=9704, name="A2", short_name="A2")
    make_fixture(fpl_id=9803, gameweek=gw, home_team_id=home, away_team_id=away,
                 kickoff_time=datetime(2099, 1, 1, tzinfo=timezone.utc), finished=False)

    body = client.get("/team", params={"season": TEST_SEASON, "gameweek": gw}, headers=bearer_headers(test_user)).json()
    assert body["live_status"] == "upcoming"


# ---------------------------------------------------------------- autosubs


def _internal_ids(engine, fpl_ids):
    with engine.connect() as conn:
        return {
            fid: conn.execute(
                text("SELECT id FROM ml.players WHERE fpl_id = :f AND season = :s"),
                {"f": fid, "s": TEST_SEASON},
            ).scalar()
            for fid in fpl_ids
        }


# _seed_full_squad lays players out as [GK, GK, DEF*5, MID*5, FWD*3]. The other
# tests in this file slice [:11] for the XI, which puts BOTH keepers on the
# pitch and leaves no GK on the bench -- harmless there (that endpoint just
# echoes starting_xi), but autosub resolution requires exactly one GK in each
# group. These indices give a legal 1-4-4-2 XI with a GK, DEF, MID and FWD
# on the bench.
_LEGAL_XI_INDICES = (0, 2, 3, 4, 5, 7, 8, 9, 10, 12, 13)
_LEGAL_BENCH_INDICES = (1, 6, 11, 14)


def test_autosub_flags_mark_both_ends_of_the_swap(
    engine, make_team, make_player, make_gw_stat, test_user
):
    """A 0-minute outfield starter replaced by a bench player who did play:
    both the player coming off and the one coming on must be flagged."""
    gw = 9107
    fpl_ids = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, id_offset=9400)
    xi_ids = [fpl_ids[i] for i in _LEGAL_XI_INDICES]
    bench_ids = [fpl_ids[i] for i in _LEGAL_BENCH_INDICES]
    _seed_gw_selection(engine, test_user, TEST_SEASON, gw, xi_ids, bench_ids,
                       captain_id=xi_ids[0], vice_captain_id=xi_ids[1])

    internal = _internal_ids(engine, fpl_ids)
    dropped = fpl_ids[10]  # a starting MID who records 0 minutes
    for fid in fpl_ids:
        make_gw_stat(internal[fid], gw, minutes=0 if fid == dropped else 90, total_points=2)

    body = client.get("/team", params={"season": TEST_SEASON, "gameweek": gw}, headers=bearer_headers(test_user)).json()
    by_id = {p["player_id"]: p
             for p in [q for group in body["lineup"].values() for q in group] + body["bench"]}

    assert by_id[dropped]["is_autosubbed_out"] is True
    assert by_id[dropped]["is_autosubbed_in"] is False

    # Exactly one replacement, and it must come off the bench. Which bench
    # player wins is scoring.py's business (bench order, then formation
    # legality) -- asserting the identity here would just re-encode that rule.
    subbed_in = [pid for pid, p in by_id.items() if p["is_autosubbed_in"]]
    assert len(subbed_in) == 1
    assert subbed_in[0] in bench_ids

    # A player who simply played their normal game carries neither flag.
    assert by_id[xi_ids[0]]["is_autosubbed_out"] is False
    assert by_id[xi_ids[0]]["is_autosubbed_in"] is False


def test_bench_boost_reports_no_autosubs(engine, make_team, make_player, make_gw_stat, test_user):
    """bench_boost counts all 15, so scoring.py skips autosub entirely -- the
    dashboard must not invent swaps that never happened."""
    gw = 9108
    fpl_ids = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, id_offset=9500)
    xi_ids = [fpl_ids[i] for i in _LEGAL_XI_INDICES]
    bench_ids = [fpl_ids[i] for i in _LEGAL_BENCH_INDICES]
    _seed_gw_selection(engine, test_user, TEST_SEASON, gw, xi_ids, bench_ids,
                       captain_id=xi_ids[0], vice_captain_id=xi_ids[1], chip_used="bench_boost")

    internal = _internal_ids(engine, fpl_ids)
    # Same 0-minute starter as the test above -- without bench_boost this would
    # trigger a swap, so a flag appearing here would be a real bug.
    for fid in fpl_ids:
        make_gw_stat(internal[fid], gw, minutes=0 if fid == fpl_ids[10] else 90, total_points=2)

    body = client.get("/team", params={"season": TEST_SEASON, "gameweek": gw}, headers=bearer_headers(test_user)).json()
    lines = [q for group in body["lineup"].values() for q in group] + body["bench"]
    assert all(p["is_autosubbed_in"] is False for p in lines)
    assert all(p["is_autosubbed_out"] is False for p in lines)


# ------------------------------------------------------ finance history
# user_gameweek_finance: a 'final' gameweek reads the frozen snapshot
# Results/scoring.py wrote at scoring time; anything not yet 'final' keeps
# reading today's live user_squads/squad_players state, unchanged.


def _seed_finance_snapshot(engine, user_id, season, gameweek, bank_tenths, team_value_tenths):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO user_gameweek_finance (user_id, season, gameweek, bank, team_value) "
                "VALUES (:u, :s, :gw, :b, :tv)"
            ),
            {"u": user_id, "s": season, "gw": gameweek, "b": bank_tenths, "tv": team_value_tenths},
        )


def _finish_gameweek_fixture(make_team, make_fixture, gameweek, id_offset):
    home = make_team(fpl_id=id_offset, name=f"Home{id_offset}", short_name=f"H{id_offset}")
    away = make_team(fpl_id=id_offset + 1, name=f"Away{id_offset}", short_name=f"A{id_offset}")
    make_fixture(
        fpl_id=id_offset + 2, gameweek=gameweek, home_team_id=home, away_team_id=away,
        kickoff_time=datetime(2020, 1, 1, tzinfo=timezone.utc), finished=True,
    )


def test_team_value_stays_live_when_gameweek_not_final(engine, make_team, make_player, test_user):
    """No fixtures ingested for this gameweek -> live_status is 'upcoming',
    not 'final' -- team_value/bank must keep reading today's live squad,
    exactly as before this feature existed, even though a (deliberately
    different) snapshot row exists for the same gameweek."""
    gw = 9109
    _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, id_offset=9600)  # live: £90.0m / bank £10.0m
    _seed_finance_snapshot(engine, test_user, TEST_SEASON, gw, bank_tenths=1, team_value_tenths=1)  # decoy

    body = client.get("/team", params={"season": TEST_SEASON, "gameweek": gw}, headers=bearer_headers(test_user)).json()

    assert body["live_status"] == "upcoming"
    assert body["team_value_available"] is True
    assert body["team_value"] == 90.0
    assert body["bank"] == (1000 - 15 * 60) / 10


def test_team_value_reads_snapshot_when_gameweek_final(engine, make_team, make_fixture, make_player, test_user):
    """Once every fixture is finished, team_value/bank must come from the
    frozen user_gameweek_finance snapshot, NOT today's live squad -- the two
    are seeded to different, distinguishable values specifically to prove
    the snapshot (not the live query) is what's actually being read."""
    gw = 9110
    _finish_gameweek_fixture(make_team, make_fixture, gw, id_offset=9601)
    _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, id_offset=9610)  # live: £90.0m / bank £10.0m
    _seed_finance_snapshot(engine, test_user, TEST_SEASON, gw, bank_tenths=25, team_value_tenths=205)

    body = client.get("/team", params={"season": TEST_SEASON, "gameweek": gw}, headers=bearer_headers(test_user)).json()

    assert body["live_status"] == "final"
    assert body["team_value_available"] is True
    assert body["team_value"] == 20.5
    assert body["bank"] == 2.5


def test_team_value_unavailable_when_final_gameweek_has_no_snapshot(engine, make_team, make_fixture, make_player, test_user):
    """A 'final' gameweek that was somehow never scored (no
    user_gameweek_finance row) reports team_value_available=False rather
    than silently falling back to today's live figures, which would
    misrepresent them as this gameweek's history."""
    gw = 9111
    _finish_gameweek_fixture(make_team, make_fixture, gw, id_offset=9611)
    _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, id_offset=9620)  # live squad exists, but no snapshot

    body = client.get("/team", params={"season": TEST_SEASON, "gameweek": gw}, headers=bearer_headers(test_user)).json()

    assert body["live_status"] == "final"
    assert body["team_value_available"] is False
    assert body["team_value"] == 0.0
    assert body["bank"] == 0.0
