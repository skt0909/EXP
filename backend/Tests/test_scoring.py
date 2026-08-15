"""
test_scoring.py — tests for Game_logic/scoring.py's score_gameweek():
raw/final point summation, GK autosub, outfield autosub with formation
legality (including the "first bench candidate would break formation,
second candidate works" case), captain/vice-captain multiplier
resolution, triple_captain/bench_boost chip effects, transfer-hit
deductions, skip-if-no-selection, idempotent re-runs, and season_total
accumulation across gameweeks.

Bypasses squad_selection.py/starting_xi.py entirely and seeds
gw_selections/starting_xi directly via SQL -- scoring.py only reads
gw_selections, starting_xi, ml.players, ml.player_gw_stats, transfers,
and gw_scores; it never touches squad_players/user_squads, so those
don't need seeding here.
"""

from sqlalchemy import text
import pytest

from conftest import TEST_SEASON
from scoring import score_gameweek

GAMEWEEK = 1


@pytest.fixture
def test_user(engine):
    """Same rationale as Tests/test_transfers.py's fixture: a user who
    accumulates transfers rows can never be DELETEd (enforce_transfers_
    immutability_fn blocks the cascade too), so this uses a unique
    identity per run and only cleans up what's actually deletable,
    leaving users/transfers rows behind permanently by design.
    """
    import uuid

    unique = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        uid = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
            {"e": f"pytest_scoring_{unique}@example.com", "u": f"pytest_scoring_{unique}", "p": "not_a_real_hash"},
        ).scalar()
    yield uid
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM gw_scores WHERE user_id = :uid"), {"uid": uid})
        conn.execute(text("DELETE FROM gw_selections WHERE user_id = :uid"), {"uid": uid})  # cascades starting_xi


def _seed_player(make_team, make_player, make_gw_stat, fpl_id, position, gameweek, minutes, total_points):
    team_id = make_team(fpl_id=fpl_id + 50000, name=f"T{fpl_id}", short_name=f"T{fpl_id}")
    internal_id = make_player(fpl_id=fpl_id, position=position, team_id=team_id, cost_start=0)
    make_gw_stat(player_id=internal_id, gameweek=gameweek, minutes=minutes, total_points=total_points)
    return fpl_id


def _build_squad(make_team, make_player, make_gw_stat, gameweek, xi_specs, bench_specs, id_offset=9000):
    """xi_specs/bench_specs: list of (position, minutes, total_points).
    Returns (xi_ids, bench_ids)."""
    all_specs = list(xi_specs) + list(bench_specs)
    fpl_ids = [
        _seed_player(make_team, make_player, make_gw_stat, id_offset + i, pos, gameweek, minutes, points)
        for i, (pos, minutes, points) in enumerate(all_specs)
    ]
    return fpl_ids[: len(xi_specs)], fpl_ids[len(xi_specs):]


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


def _add_transfer_hits(engine, user_id, season, gameweek, n, id_offset=90000):
    with engine.begin() as conn:
        for i in range(n):
            conn.execute(
                text(
                    "INSERT INTO transfers (user_id, season, gameweek, player_in_id, player_out_id, price_in, price_out, is_free) "
                    "VALUES (:u, :s, :gw, :pin, :pout, 50, 50, FALSE)"
                ),
                {"u": user_id, "s": season, "gw": gameweek, "pin": id_offset + i, "pout": id_offset + 100 + i},
            )


def _gw_score_row(engine, user_id, season, gameweek):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT raw_points, final_points, transfer_hits, hit_deductions, total_points, season_total "
                "FROM gw_scores WHERE user_id = :u AND season = :s AND gameweek = :gw"
            ),
            {"u": user_id, "s": season, "gw": gameweek},
        ).first()


# A 1GK/4DEF/4MID/2FWD XI + a 4-man bench (1GK/1DEF/1MID/1FWD), all 3
# points/90 minutes -- used by tests that don't care about the exact
# numbers, only that everyone played and nothing needs an autosub.
SIMPLE_XI = [("GK", 90, 3)] + [("DEF", 90, 3)] * 4 + [("MID", 90, 3)] * 4 + [("FWD", 90, 3)] * 2
SIMPLE_BENCH = [("GK", 0, 0), ("DEF", 0, 0), ("MID", 0, 0), ("FWD", 0, 0)]  # didn't play, irrelevant here


def test_simple_valid_case_no_autosub(engine, make_team, make_player, make_gw_stat, test_user):
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, SIMPLE_XI, SIMPLE_BENCH)
    _seed_gw_selection(engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, xi_ids[0], xi_ids[1])

    summary = score_gameweek(engine, TEST_SEASON, GAMEWEEK)

    assert test_user in summary["scored"]
    assert summary["failed"] == []
    row = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    assert row.raw_points == 33  # 11 * 3
    assert row.final_points == 36  # captain (GK, 3 pts) -> +3
    assert row.transfer_hits == 0
    assert row.hit_deductions == 0
    assert row.total_points == 36
    assert row.season_total == 36


def test_gk_autosub_swaps_in_bench_gk(engine, make_team, make_player, make_gw_stat, test_user):
    xi_specs = [("GK", 0, 0)] + [("DEF", 90, 2)] * 4 + [("MID", 90, 2)] * 4 + [("FWD", 90, 2)] * 2  # 10 outfield * 2 = 20
    bench_specs = [("DEF", 0, 0), ("MID", 0, 0), ("GK", 90, 6), ("FWD", 0, 0)]  # bench GK played
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, xi_specs, bench_specs)
    captain = xi_ids[1]  # an outfield player, unaffected by the GK swap
    _seed_gw_selection(engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, captain, xi_ids[2])

    score_gameweek(engine, TEST_SEASON, GAMEWEEK)

    row = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    assert row.raw_points == 26  # 20 (outfield) + 6 (bench GK swapped in)
    assert row.final_points == 28  # captain 2 pts -> +2


def test_outfield_autosub_respects_formation(engine, make_team, make_player, make_gw_stat, test_user):
    xi_specs = [
        ("GK", 90, 2),
        ("DEF", 90, 3), ("DEF", 90, 4), ("DEF", 0, 0),  # 3rd DEF didn't play
        ("MID", 90, 5), ("MID", 90, 6), ("MID", 90, 7), ("MID", 90, 8),
        ("FWD", 90, 9), ("FWD", 90, 10), ("FWD", 90, 11),
    ]  # 1GK + 3DEF + 4MID + 3FWD = 11, DEF at its floor (3)
    bench_specs = [
        ("MID", 90, 999),  # priority 1 -- swapping this in for the DEF would drop DEF to 2 -- illegal, must be skipped
        ("DEF", 90, 99),   # priority 2 -- DEF stays at 3 -- legal, must be the one used
        ("GK", 0, 0),
        ("FWD", 90, 888),  # priority 3 (post-GK) -- never reached, DEF candidate already filled the gap
    ]
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, xi_specs, bench_specs)
    captain = xi_ids[0]  # GK, 2 pts
    _seed_gw_selection(engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, captain, xi_ids[1])

    score_gameweek(engine, TEST_SEASON, GAMEWEEK)

    row = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    # 2 (GK) + 3+4 (working DEF) + 99 (DEF candidate, NOT 999/888) + 26 (MID) + 30 (FWD) = 164
    assert row.raw_points == 164
    assert row.final_points == 166  # captain 2 pts -> +2


def test_no_valid_autosub_stays_at_zero(engine, make_team, make_player, make_gw_stat, test_user):
    xi_specs = [
        ("GK", 90, 2),
        ("DEF", 90, 3), ("DEF", 90, 4), ("DEF", 0, 0),  # 0-minute DEF, nobody eligible to replace them
        ("MID", 90, 5), ("MID", 90, 6), ("MID", 90, 7), ("MID", 90, 8),
        ("FWD", 90, 9), ("FWD", 90, 10), ("FWD", 90, 11),
    ]
    bench_specs = [("MID", 0, 0), ("DEF", 0, 0), ("GK", 0, 0), ("FWD", 0, 0)]  # bench entirely 0-minute
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, xi_specs, bench_specs)
    captain = xi_ids[0]
    _seed_gw_selection(engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, captain, xi_ids[1])

    summary = score_gameweek(engine, TEST_SEASON, GAMEWEEK)
    assert summary["failed"] == []  # no crash

    row = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    # 2 (GK) + 3+4+0 (0-min DEF stays, contributes 0) + 26 (MID) + 30 (FWD) = 65
    assert row.raw_points == 65
    assert row.final_points == 67  # captain 2 pts -> +2


def test_captain_played_gets_2x(engine, make_team, make_player, make_gw_stat, test_user):
    xi_specs = [("GK", 90, 1)] + [("DEF", 90, 1)] * 4 + [("MID", 90, 1)] * 4 + [("FWD", 90, 1), ("FWD", 90, 10)]
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, xi_specs, SIMPLE_BENCH)
    captain = xi_ids[-1]  # the 10-point FWD
    _seed_gw_selection(engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, captain, xi_ids[0])

    score_gameweek(engine, TEST_SEASON, GAMEWEEK)

    row = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    assert row.final_points - row.raw_points == 10  # captain's own points added once more


def test_captain_zero_minutes_vice_gets_multiplier(engine, make_team, make_player, make_gw_stat, test_user):
    xi_specs = [
        ("GK", 90, 1),
        ("DEF", 90, 2), ("DEF", 90, 2), ("DEF", 90, 2),
        ("MID", 90, 3), ("MID", 90, 3), ("MID", 90, 3), ("MID", 90, 3),
        ("FWD", 0, 7),   # captain: 0 minutes -- deliberately nonzero total_points on record, to prove the
                         # multiplier gate is minutes, not total_points (their own 7 still counts once, just no bonus)
        ("FWD", 90, 8),  # vice-captain: played
        ("FWD", 90, 9),
    ]
    bench_specs = [("GK", 0, 0), ("DEF", 0, 0), ("MID", 0, 0), ("FWD", 0, 0)]  # no eligible subs -- isolates captain/vice logic from autosub
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, xi_specs, bench_specs)
    captain, vice = xi_ids[8], xi_ids[9]  # the 0-min FWD (index 8) and the played FWD (index 9)
    _seed_gw_selection(engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, captain, vice)

    score_gameweek(engine, TEST_SEASON, GAMEWEEK)

    row = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    # 1 (GK) + 6 (DEF) + 12 (MID) + 7 (captain, own points, no bonus) + 8 (vice) + 9 (FWD3) = 43
    assert row.raw_points == 43
    assert row.final_points == 43 + 8  # vice's own 8 added once more; captain gets no bonus at all


def test_neither_captain_nor_vice_played_no_multiplier(engine, make_team, make_player, make_gw_stat, test_user):
    xi_specs = [
        ("GK", 90, 1),
        ("DEF", 90, 2), ("DEF", 90, 2), ("DEF", 90, 2),
        ("MID", 90, 3), ("MID", 90, 3), ("MID", 90, 3), ("MID", 90, 3),
        ("FWD", 0, 7),  # captain: 0 minutes
        ("FWD", 0, 8),  # vice: also 0 minutes
        ("FWD", 90, 9),
    ]
    bench_specs = [("GK", 0, 0), ("DEF", 0, 0), ("MID", 0, 0), ("FWD", 0, 0)]
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, xi_specs, bench_specs)
    captain, vice = xi_ids[8], xi_ids[9]  # both 0-min FWDs (indices 8 and 9)
    _seed_gw_selection(engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, captain, vice)

    score_gameweek(engine, TEST_SEASON, GAMEWEEK)

    row = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    assert row.final_points == row.raw_points


def test_triple_captain_chip_applies_3x(engine, make_team, make_player, make_gw_stat, test_user):
    xi_specs = [("GK", 90, 1)] + [("DEF", 90, 1)] * 4 + [("MID", 90, 1)] * 4 + [("FWD", 90, 1), ("FWD", 90, 10)]
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, xi_specs, SIMPLE_BENCH)
    captain = xi_ids[-1]  # 10 points
    _seed_gw_selection(
        engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, captain, xi_ids[0], chip_used="triple_captain"
    )

    score_gameweek(engine, TEST_SEASON, GAMEWEEK)

    row = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    assert row.final_points - row.raw_points == 20  # (3 - 1) * 10


def test_bench_boost_chip_counts_all_15(engine, make_team, make_player, make_gw_stat, test_user):
    xi_specs = [("GK", 90, 1)] + [("DEF", 90, 2)] * 4 + [("MID", 90, 3)] * 4 + [("FWD", 90, 4)] * 2  # sum = 29
    bench_specs = [("GK", 90, 5), ("DEF", 90, 6), ("MID", 90, 7), ("FWD", 90, 8)]  # sum = 26, normally ignored
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, xi_specs, bench_specs)
    _seed_gw_selection(
        engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, xi_ids[0], xi_ids[1], chip_used="bench_boost"
    )

    score_gameweek(engine, TEST_SEASON, GAMEWEEK)

    row = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    assert row.raw_points == 55  # 29 + 26 -- bench counted despite never being "subbed on"
    assert row.final_points == 56  # captain (GK, 1 pt) -> +1


def test_transfer_hits_deduct_points(engine, make_team, make_player, make_gw_stat, test_user):
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, SIMPLE_XI, SIMPLE_BENCH)
    _seed_gw_selection(engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, xi_ids[0], xi_ids[1])
    _add_transfer_hits(engine, test_user, TEST_SEASON, GAMEWEEK, n=2)

    score_gameweek(engine, TEST_SEASON, GAMEWEEK)

    row = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    assert row.transfer_hits == 2
    assert row.hit_deductions == 8
    assert row.final_points == 36  # unaffected by hits
    assert row.total_points == 36 - 8


def test_user_without_gw_selection_is_skipped(engine, test_user):
    summary = score_gameweek(engine, TEST_SEASON, GAMEWEEK)

    assert test_user not in summary["scored"]
    assert _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK) is None


def test_rerunning_is_idempotent(engine, make_team, make_player, make_gw_stat, test_user):
    xi_ids, bench_ids = _build_squad(make_team, make_player, make_gw_stat, GAMEWEEK, SIMPLE_XI, SIMPLE_BENCH)
    _seed_gw_selection(engine, test_user, TEST_SEASON, GAMEWEEK, xi_ids, bench_ids, xi_ids[0], xi_ids[1])

    score_gameweek(engine, TEST_SEASON, GAMEWEEK)
    row1 = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)
    score_gameweek(engine, TEST_SEASON, GAMEWEEK)
    row2 = _gw_score_row(engine, test_user, TEST_SEASON, GAMEWEEK)

    assert tuple(row1) == tuple(row2)
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM gw_scores WHERE user_id = :u AND season = :s AND gameweek = :gw"),
            {"u": test_user, "s": TEST_SEASON, "gw": GAMEWEEK},
        ).scalar()
    assert count == 1


def test_season_total_sums_across_gameweeks(engine, make_team, make_player, make_gw_stat, test_user):
    xi_ids1, bench_ids1 = _build_squad(make_team, make_player, make_gw_stat, 1, SIMPLE_XI, SIMPLE_BENCH, id_offset=9000)
    _seed_gw_selection(engine, test_user, TEST_SEASON, 1, xi_ids1, bench_ids1, xi_ids1[0], xi_ids1[1])
    score_gameweek(engine, TEST_SEASON, 1)
    row1 = _gw_score_row(engine, test_user, TEST_SEASON, 1)

    xi_ids2, bench_ids2 = _build_squad(make_team, make_player, make_gw_stat, 2, SIMPLE_XI, SIMPLE_BENCH, id_offset=9100)
    _seed_gw_selection(engine, test_user, TEST_SEASON, 2, xi_ids2, bench_ids2, xi_ids2[0], xi_ids2[1])
    score_gameweek(engine, TEST_SEASON, 2)
    row2 = _gw_score_row(engine, test_user, TEST_SEASON, 2)

    assert row1.total_points == 36
    assert row2.total_points == 36
    assert row2.season_total == row1.total_points + row2.total_points  # 72, not just gw2's own 36
