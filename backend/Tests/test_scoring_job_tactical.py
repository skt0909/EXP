"""Phase 4a: the batched tactical scoring job.

Covers what only a real database can answer: batching, the epoch gate, the
season filter, the finance write, and that the numbers the engine produces
actually land in gw_scores. The engine's own arithmetic is tested without a
database in backend/Tests/unit/.

The golden gameweek in test (a) is hand-computed in the comments beside it, so
a change to the point values fails here with the arithmetic written out.
"""
import logging
import re

import pytest
from sqlalchemy import text

from conftest import TEST_SEASON
from Results.scoring_job import SCORING_BATCH_SIZE, score_gameweek_tactical
from Shared.rules import RULES_VERSION

GAMEWEEK = 5
BASE = 6100


# ---- seeding ---------------------------------------------------------------

def _squad_for(engine, make_team, make_player, make_fixture, user_id, offset,
               tactic, bonus_idx, swaps=(), gameweek=GAMEWEEK):
    """One manager with a 15-man squad and a submitted selection.

    Layout, by index into the 15: 0-1 GK, 2-6 DEF, 7-11 MID, 12-14 FWD.
    XI is 1-4-4-2 -> indices 0, 2,3,4,5, 7,8,9,10, 12,13.
    Bench slots 12-15 -> indices 1 (GK), 6 (DEF), 11 (MID), 14 (FWD).

    Returns a list of (fpl_id, internal_id) pairs. BOTH are needed and they
    are not interchangeable: squad_players and starting_xi store the FPL id,
    while ml.player_gw_stats keys on ml.players.id, which is what make_player
    returns. Conflating them silently produces a squad whose players have no
    stats -- every score comes out 0 and nothing errors.
    """
    shape = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    pairs = []
    for i, position in enumerate(shape):
        team = make_team(fpl_id=offset + 500 + i, name=f"T{offset}_{i}",
                         short_name=f"{(offset + i) % 1000:03d}")
        internal = make_player(fpl_id=offset + i, position=position,
                               team_id=team, cost_start=50)
        pairs.append((offset + i, internal))

    ids = [fpl for fpl, _ in pairs]
    xi = [ids[0], ids[2], ids[3], ids[4], ids[5], ids[7], ids[8], ids[9], ids[10], ids[12], ids[13]]
    bench = [ids[1], ids[6], ids[11], ids[14]]

    with engine.begin() as conn:
        sq = conn.execute(text(
            "INSERT INTO user_squads (user_id, season, budget_remaining) "
            "VALUES (:u, :s, 35) RETURNING id"), {"u": user_id, "s": TEST_SEASON}).scalar()
        for pid in ids:
            conn.execute(text(
                "INSERT INTO squad_players (user_squad_id, player_id, purchase_price, is_active) "
                "VALUES (:sq, :p, 50, TRUE)"), {"sq": sq, "p": pid})

        sel = conn.execute(text(
            "INSERT INTO gw_selections (user_id, season, gameweek, tactic, submitted_at) "
            "VALUES (:u, :s, :g, :t, now()) RETURNING id"),
            {"u": user_id, "s": TEST_SEASON, "g": gameweek, "t": tactic}).scalar()
        bonus = {ids[i] for i in bonus_idx}
        for slot, pid in enumerate(xi + bench, start=1):
            conn.execute(text(
                "INSERT INTO starting_xi (gw_selection_id, player_id, position_slot, is_bonus) "
                "VALUES (:sel, :p, :slot, :b)"),
                {"sel": sel, "p": pid, "slot": slot, "b": pid in bonus})
        for out_i, in_i in swaps:
            conn.execute(text(
                "INSERT INTO tactical_swaps (gw_selection_id, player_out_id, player_in_id) "
                "VALUES (:sel, :o, :i)"), {"sel": sel, "o": ids[out_i], "i": ids[in_i]})
    return pairs


def _stat(engine, make_gw_stat, pair, gameweek=GAMEWEEK, **kw):
    """`pair` is (fpl_id, internal_id); make_gw_stat keys on the internal one."""
    make_gw_stat(player_id=pair[1], gameweek=gameweek, **kw)


def _seed_stats(make_gw_stat, pairs, overrides=None, gameweek=GAMEWEEK):
    """Seed EXACTLY ONE stats row per player.

    ml.player_gw_stats has uq_pgws_player_season_gw_fixture, so the tempting
    "give everyone 90 minutes, then override the interesting ones" pattern
    inserts twice for the overridden players and fails. Overrides are merged
    BEFORE the single insert instead.

    `overrides` maps an index into `pairs` to the row for that player.
    """
    overrides = overrides or {}
    for i, pair in enumerate(pairs):
        row = dict(minutes=90)
        row.update(overrides.get(i, {}))
        make_gw_stat(player_id=pair[1], gameweek=gameweek, **row)


def _score_row(engine, user_id, gameweek=GAMEWEEK):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT raw_points, tactical_points, sub_bonus, final_points, total_points, "
            "season_total, rules_version FROM gw_scores "
            "WHERE user_id = :u AND season = :s AND gameweek = :g"),
            {"u": user_id, "s": TEST_SEASON, "g": gameweek}).first()


# ---- (h) a manager with no selection gets no row --------------------------

def test_a_manager_with_no_selection_gets_no_score_row(engine, make_user):
    """Evidence for 'exactly as today': the job's driving query selects FROM
    gw_selections, so a manager without a row is never in the result set at
    all -- not skipped by a condition that could be got wrong."""
    stranger = make_user()
    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert stranger not in summary["scored"]
    assert _score_row(engine, stranger) is None


# ---- (d) season filter -----------------------------------------------------

@pytest.mark.parametrize("sim_season", ["SIM38OK", "SIM38TST", "SIMSMOKE"])
def test_simulation_seasons_are_never_scored(engine, sim_season):
    summary = score_gameweek_tactical(engine, sim_season, GAMEWEEK)
    assert summary["scored"] == []
    assert summary["skipped_reason"] is not None
    assert "season" in summary["skipped_reason"]


def test_the_real_season_pattern_is_what_decides():
    from Results.scoring_job import REAL_SEASON_RE
    assert re.fullmatch(REAL_SEASON_RE, "2026-27")
    assert re.fullmatch(REAL_SEASON_RE, TEST_SEASON)      # 2099-00 is real-shaped
    for bad in ("SIM38OK", "SIM38TST", "SIMSMOKE", "2026", "2026-2027"):
        assert not re.fullmatch(REAL_SEASON_RE, bad), bad


# ---- (e) the epoch gate ----------------------------------------------------

@pytest.fixture
def epoch(engine):
    made = []

    def _set(first_gameweek):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO ruleset_epochs (season, rules_version, first_gameweek) "
                "VALUES (:s, :rv, :fg) ON CONFLICT DO NOTHING"),
                {"s": TEST_SEASON, "rv": RULES_VERSION, "fg": first_gameweek})
        made.append(True)

    yield _set
    if made:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE ruleset_epochs DISABLE TRIGGER enforce_ruleset_epochs_immutability"))
            conn.execute(text("DELETE FROM ruleset_epochs WHERE season = :s"), {"s": TEST_SEASON})
            conn.execute(text("ALTER TABLE ruleset_epochs ENABLE TRIGGER enforce_ruleset_epochs_immutability"))


def test_a_gameweek_before_the_epoch_is_not_scored(engine, make_user, make_team,
                                                   make_player, make_fixture, epoch):
    epoch(GAMEWEEK + 2)
    uid = make_user()
    _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 7000,
               "balanced", (7, 8))
    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert summary["scored"] == []
    assert "epoch" in (summary["skipped_reason"] or "")
    assert _score_row(engine, uid) is None


def test_no_epoch_row_means_score_from_gameweek_one(engine, make_user, make_team,
                                                    make_player, make_fixture):
    uid = make_user()
    _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 7200,
               "balanced", (7, 8))
    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert uid in summary["scored"]


# ---- (a) the golden gameweek ----------------------------------------------

def test_golden_gameweek_three_managers_three_tactics(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """Hand-computed. Every player plays 90 minutes and scores nothing unless
    named, so each manager's baseline is 11 starters x 2 appearance = 22.

    Manager A -- BALANCED, Bonus = the two midfielders at indices 7 and 8.
      index 7  MID: 1 goal, creativity 40  -> general 2+5 = 7, tactical 1 + 3 = 4
      index 8  MID: nothing                -> general 2,     tactical 0
      general = 22 - 2 + 7 = 27   tactical = 4   sub_bonus = 0
      final = 31, total = 31

    Manager B -- DEFENCE, Bonus = the two defenders at indices 2 and 3,
                 plus a SWAP (MID index 10 out, bench MID index 11 in) and an
                 AUTO SUB (DEF index 4 did not play; bench DEF index 6 covers).
      index 2  DEF: clean sheet, 10 defensive actions
                    -> general 2 + 4 (cs) + 2 (DC>=10) = 8
                    -> tactical 2 (cs) + 3 (DC tier 10+) = 5
      index 3  DEF: nothing -> general 2, tactical 0
      index 4  DEF: 0 minutes -> replaced, contributes nothing
      index 6  DEF (bench 13): 90 min, 1 goal -> general 2 + 6 = 8, covers index 4
      index 10 MID: swapped out, 90 min, nothing -> general 2, BANKED
      index 11 MID (bench 14): swapped in, 1 goal -> general 2 + 5 = 7
      Starters counted: 11 - 1 (replaced) = 10, plus cover, plus swap-in.
      general = (22 - 2 - 2 - 2) + 8 + 8 + 7 = 16 + 23 = 39
                 ^ remove index 2, 4, 10 from the flat 22 baseline, add their real values
      Recomputed explicitly: 8 other starters x 2 = 16, index 2 = 8,
                             index 4 = 0 (replaced), index 10 = 2 (banked),
                             cover index 6 = 8, swap-in index 11 = 7
                             -> 16 + 8 + 0 + 2 + 8 + 7 = 41
      tactical = 5      sub_bonus = 1 (7 > 2)
      final = 41 + 5 + 1 = 47, total = 47

    Manager C -- ATTACK, Bonus = the two forwards at indices 12 and 13, and
                 forward index 12 is a NO-SHOW, so he loses Bonus status.
      index 12 FWD: 0 minutes -> general 0, tactical 0 (no-show loses Bonus)
      index 13 FWD: 2 goals   -> general 2 + 8 = 10, tactical 2 x 3 = 6
      The bench Auto Sub (index 6, a DEF) covers index 12? Covering a FWD with
      a DEF leaves 1 FWD and 5 DEF -- FWD >= 1 holds and DEF max is implied by
      the squad, so the cover IS legal and index 6 comes on.
      index 6 DEF (bench 13): 90 min, nothing -> general 2
      general = 9 other starters x 2 = 18, index 12 = 0 (replaced),
                index 13 = 10, cover index 6 = 2  -> 18 + 0 + 10 + 2 = 30
      tactical = 6      sub_bonus = 0
      final = 36, total = 36
    """
    a, b, c = make_user(), make_user(), make_user()

    ids_a = _squad_for(engine, make_team, make_player, make_fixture, a, BASE, "balanced", (7, 8))
    ids_b = _squad_for(engine, make_team, make_player, make_fixture, b, BASE + 100, "defence",
                       (2, 3), swaps=[(10, 11)])
    ids_c = _squad_for(engine, make_team, make_player, make_fixture, c, BASE + 200, "attack", (12, 13))

    # Everyone plays 90 and does nothing, except the named exceptions. One
    # row per player, merged before insert -- see _seed_stats.
    _seed_stats(make_gw_stat, ids_a, {
        7: dict(minutes=90, goals_scored=1, creativity=40),
    })
    _seed_stats(make_gw_stat, ids_b, {
        2: dict(minutes=90, clean_sheets=1, defensive_contributions=10),
        4: dict(minutes=0),
        6: dict(minutes=90, goals_scored=1),
        11: dict(minutes=90, goals_scored=1),
    })
    _seed_stats(make_gw_stat, ids_c, {
        12: dict(minutes=0),
        13: dict(minutes=90, goals_scored=2),
    })

    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    # Scoped to OUR three managers: this database is shared with every other
    # test file, and a leftover selection from one of them failing is not this
    # test's business.
    ours_failed = [f for f in summary["failed"] if f[0] in (a, b, c)]
    assert ours_failed == [], ours_failed
    assert set(summary["scored"]) >= {a, b, c}

    ra, rb, rc = _score_row(engine, a), _score_row(engine, b), _score_row(engine, c)

    assert (ra.raw_points, ra.tactical_points, ra.sub_bonus) == (27, 4, 0)
    assert (ra.final_points, ra.total_points) == (31, 31)

    assert (rb.raw_points, rb.tactical_points, rb.sub_bonus) == (41, 5, 1)
    assert (rb.final_points, rb.total_points) == (47, 47)

    assert (rc.raw_points, rc.tactical_points, rc.sub_bonus) == (30, 6, 0)
    assert (rc.final_points, rc.total_points) == (36, 36)

    for r in (ra, rb, rc):
        assert r.rules_version == RULES_VERSION == 3


# ---- (b) idempotent --------------------------------------------------------

def test_running_twice_produces_identical_rows(engine, make_user, make_team,
                                               make_player, make_fixture, make_gw_stat):
    uid = make_user()
    ids = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 300,
                     "balanced", (7, 8))
    _seed_stats(make_gw_stat, ids)

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    first = _score_row(engine, uid)
    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    second = _score_row(engine, uid)

    assert tuple(first) == tuple(second)
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM gw_scores WHERE user_id = :u AND season = :s AND gameweek = :g"),
            {"u": uid, "s": TEST_SEASON, "g": GAMEWEEK}).scalar()
    assert n == 1


# ---- (c) batch boundary ----------------------------------------------------

def test_batch_size_does_not_change_the_result(engine, make_user, make_team,
                                               make_player, make_fixture, make_gw_stat):
    users, all_ids = [], []
    for i in range(5):
        uid = make_user()
        users.append(uid)
        ids = _squad_for(engine, make_team, make_player, make_fixture, uid,
                         BASE + 1000 + i * 50, "balanced", (7, 8))
        all_ids.append(ids)
        # One manager scores something, so the comparison is not all-zeroes.
        extra = {7: dict(minutes=90, goals_scored=1, creativity=40)} if i == 3 else {}
        _seed_stats(make_gw_stat, ids, extra)

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK, batch_size=2)
    small = {u: tuple(_score_row(engine, u)) for u in users}

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK, batch_size=SCORING_BATCH_SIZE)
    large = {u: tuple(_score_row(engine, u)) for u in users}

    assert small == large
    assert any(v[0] > 22 for v in small.values()), "expected one manager above the baseline"


# ---- (f) an invalid stored selection is still scored -----------------------

def test_an_invalid_stored_selection_is_scored_and_logs_a_warning(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, caplog
):
    """Decision A7: a data bug must never leave a manager with no score."""
    uid = make_user()
    ids = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 400,
                     "balanced", (7, 8))
    _seed_stats(make_gw_stat, ids)

    # Break it behind the validator's back: a THIRD bonus player. The deferred
    # trigger only fires on write, so an UPDATE with it disabled leaves stored
    # data the validator would reject.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE starting_xi DISABLE TRIGGER enforce_bonus_count"))
        conn.execute(text(
            "UPDATE starting_xi SET is_bonus = TRUE WHERE player_id = :p "
            "AND gw_selection_id IN (SELECT id FROM gw_selections WHERE user_id = :u)"),
            {"p": ids[9][0], "u": uid})
        conn.execute(text("ALTER TABLE starting_xi ENABLE TRIGGER enforce_bonus_count"))

    with caplog.at_level(logging.WARNING):
        summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)

    assert uid in summary["scored"], "an invalid selection must STILL be scored"
    assert _score_row(engine, uid) is not None
    assert any("data-integrity" in r.getMessage().lower() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


# ---- (g) the finance write is preserved -----------------------------------

def test_user_gameweek_finance_is_still_written(engine, make_user, make_team,
                                                make_player, make_fixture, make_gw_stat):
    uid = make_user()
    ids = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 500,
                     "balanced", (7, 8))
    _seed_stats(make_gw_stat, ids)

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT bank, team_value FROM user_gameweek_finance "
            "WHERE user_id = :u AND season = :s AND gameweek = :g"),
            {"u": uid, "s": TEST_SEASON, "g": GAMEWEEK}).first()
    assert row is not None, "the finance snapshot must survive the rewrite"
    # budget_remaining 35, 15 active players at purchase_price 50 = 750.
    assert row.bank == 35
    assert row.team_value == 750


def test_the_finance_write_is_also_idempotent(engine, make_user, make_team,
                                              make_player, make_fixture, make_gw_stat):
    uid = make_user()
    ids = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 600,
                     "balanced", (7, 8))
    _seed_stats(make_gw_stat, ids)

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)

    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM user_gameweek_finance "
            "WHERE user_id = :u AND season = :s AND gameweek = :g"),
            {"u": uid, "s": TEST_SEASON, "g": GAMEWEEK}).scalar()
    assert n == 1


# ---- E5: season_total must not sum across rule generations ----------------

def test_season_total_ignores_gameweeks_before_the_ruleset_epoch(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, epoch
):
    """A season that switches rulesets mid-way had a cumulative total spanning
    two scoring systems: season_total was SUM(total_points) over every earlier
    gameweek, including rows written under rules_version 1 and 2."""
    epoch(GAMEWEEK)                      # these rules begin at GAMEWEEK
    uid = make_user()
    ids = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 8000,
                     "balanced", (7, 8))
    _seed_stats(make_gw_stat, ids)

    # Two old rows under the previous rulesets, below the epoch.
    with engine.begin() as conn:
        for gw, rv, pts in ((GAMEWEEK - 2, 1, 500), (GAMEWEEK - 1, 2, 700)):
            conn.execute(text(
                "INSERT INTO gw_scores (user_id, season, gameweek, raw_points, "
                "final_points, total_points, season_total, rules_version) "
                "VALUES (:u, :s, :g, 0, 0, :p, :p, :rv)"),
                {"u": uid, "s": TEST_SEASON, "g": gw, "p": pts, "rv": rv})

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    row = _score_row(engine, uid)

    # 11 starters x 2 = 22, and NOT 22 + 500 + 700.
    assert row.season_total == row.total_points == 22


def test_without_an_epoch_row_season_total_still_sums_every_earlier_gameweek(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """No epoch row means the season has only ever known these rules, so the
    behaviour is unchanged from before this fix."""
    uid = make_user()
    ids = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 8200,
                     "balanced", (7, 8))
    _seed_stats(make_gw_stat, ids)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO gw_scores (user_id, season, gameweek, raw_points, "
            "final_points, total_points, season_total, rules_version) "
            "VALUES (:u, :s, :g, 0, 0, 30, 30, 3)"),
            {"u": uid, "s": TEST_SEASON, "g": GAMEWEEK - 1})

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    row = _score_row(engine, uid)
    assert row.season_total == 22 + 30


# ---- E7: total_points and final_points are equal under these rules --------

def test_total_points_equals_final_points_because_there_are_no_hits(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    uid = make_user()
    ids = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 8400,
                     "balanced", (7, 8))
    _seed_stats(make_gw_stat, ids, {7: dict(minutes=90, goals_scored=1, creativity=40)})
    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    row = _score_row(engine, uid)
    assert row.final_points == row.total_points
    assert row.total_points == row.raw_points + row.tactical_points + row.sub_bonus


# ---- migrated from the deleted test_scoring.py ----------------------------
#
# These four checked behaviour the tactical engine must still have, end to end
# through the job rather than against the pure engine (which
# backend/Tests/unit/test_tactical_scoring.py already covers).

def test_migrated_gk_autosub_swaps_in_the_bench_keeper(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """From test_gk_autosub_swaps_in_bench_gk."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 9000,
                       "balanced", (7, 8))
    _seed_stats(make_gw_stat, pairs, {
        0: dict(minutes=0),                       # starting GK did not play
        1: dict(minutes=90, saves=3),             # bench GK did, 2 + 1 = 3
    })
    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    # 10 outfield starters at 2 each, plus the bench keeper's 3.
    assert _score_row(engine, uid).raw_points == 23


def test_migrated_outfield_autosub_respects_formation(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """From test_outfield_autosub_respects_formation. The XI is 1-4-4-2, so
    losing a defender still leaves 3 and the DEF on slot 13 can come on."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 9100,
                       "balanced", (7, 8))
    _seed_stats(make_gw_stat, pairs, {
        4: dict(minutes=0),                       # a starting DEF blanked
        6: dict(minutes=90, goals_scored=1),      # bench DEF: 2 + 6 = 8
    })
    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert _score_row(engine, uid).raw_points == 10 * 2 + 8


def test_migrated_no_valid_autosub_leaves_the_slot_at_zero(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """From test_no_valid_autosub_stays_at_zero: the only Auto Sub also
    blanked, so nobody comes on and the slot simply scores 0."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 9200,
                       "balanced", (7, 8))
    _seed_stats(make_gw_stat, pairs, {4: dict(minutes=0), 6: dict(minutes=0)})
    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert _score_row(engine, uid).raw_points == 10 * 2


def test_migrated_a_manager_with_no_user_squads_row_scores_without_a_finance_snapshot(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """From test_no_user_squads_row_skips_finance_snapshot_without_error.

    The finance snapshot reads user_squads. A selection can exist without one
    (test data, or a squad deleted out from under it), and that must not stop
    the manager being scored -- it only means there is nothing to snapshot.
    """
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 9300,
                       "balanced", (7, 8))
    _seed_stats(make_gw_stat, pairs)
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM squad_players WHERE user_squad_id IN "
            "(SELECT id FROM user_squads WHERE user_id = :u)"), {"u": uid})
        conn.execute(text("DELETE FROM user_squads WHERE user_id = :u"), {"u": uid})

    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert uid in summary["scored"]
    assert _score_row(engine, uid).raw_points == 22

    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM user_gameweek_finance WHERE user_id = :u "
            "AND season = :s AND gameweek = :g"),
            {"u": uid, "s": TEST_SEASON, "g": GAMEWEEK}).scalar()
    assert n == 0, "no squad means nothing to snapshot, not an error"
