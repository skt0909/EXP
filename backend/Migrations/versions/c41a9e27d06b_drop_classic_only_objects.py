"""Drop classic-only objects: chips, captaincy, transfer hits. Reset gameplay data.

DRAFT built from the v1.0 DDL plus ARCHITECTURE.md. Verify against the live
schema (pg_dump --schema-only) before running.

Revision ID: c41a9e27d06b
Revises: a06f58d93f5a, c2f6a83e91d4
Create Date: 2026-09-19

This revision also acts as the MERGE point for the two divergent heads
reported by `alembic heads` (Dream11 branch and rolling_points branch).
"""
import os

import sqlalchemy as sa
from alembic import op

revision = "c41a9e27d06b"
# Tuple = merge revision. Both heads were confirmed live on 2026-09-20.
down_revision = ("a06f58d93f5a", "c2f6a83e91d4")
branch_labels = None
depends_on = None


def _refuse_to_wipe_real_data() -> None:
    """Safety guard. This revision empties gameplay tables. It only does so on an
    empty database, or when the operator opts in by name:

        ALLOW_GAMEPLAY_WIPE=<exact database name> alembic upgrade head

    Anywhere else (for example a deployed server that holds real users) it stops
    BEFORE changing anything."""
    conn = op.get_bind()
    db = conn.execute(sa.text("SELECT current_database()")).scalar()
    rows = 0
    for table in (
        "gw_selections",
        "gw_scores",
        "transfers",
        "leaderboard_snapshots",
        # Added in Phase 1: both are now wiped, so both must count towards the
        # guard. Leaving them out would let a database holding only finance or
        # cancellation rows pass the check and then lose them.
        "cancelled_transfers",
        "user_gameweek_finance",
    ):
        rows += conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
    # league_h2h_fixtures is counted CONDITIONALLY, unlike the tables above:
    # this revision resets only its three derived columns and leaves the
    # pairings alone, so an unplayed row loses nothing and must not trip the
    # guard. A resolved row (result IS NOT NULL) does lose data, so it counts.
    rows += conn.execute(
        sa.text("SELECT COUNT(*) FROM league_h2h_fixtures WHERE result IS NOT NULL")
    ).scalar() or 0
    if rows and os.environ.get("ALLOW_GAMEPLAY_WIPE") != db:
        raise RuntimeError(
            f"Refusing to run: database '{db}' holds {rows} gameplay rows and this "
            f"revision would wipe them. If this is a dev database you are happy to "
            f"empty, re-run with ALLOW_GAMEPLAY_WIPE={db}"
        )


def upgrade() -> None:
    _refuse_to_wipe_real_data()

    # ------------------------------------------------------------------
    # 1. Reset gameplay data (approved: dev data only).
    #    TRUNCATE is not blocked by the row-level immutability triggers.
    #    No CASCADE on purpose: if a live-only table (e.g. a transfer
    #    cancellation table or league_h2h_fixtures) references one of
    #    these, Postgres will error and name it. Add that table to the
    #    list below instead of cascading blindly.
    # ------------------------------------------------------------------
    # cancelled_transfers is in this list because it MUST be: it carries
    # cancelled_transfers_transfer_id_fkey -> transfers(id), and Postgres
    # refuses to truncate a table referenced by a foreign key from a table
    # that is not also being truncated. Without it this statement aborts and
    # the whole revision fails. (Verified in Phase 0 against the live schema.)
    #
    # user_gameweek_finance IS truncated, reversing the earlier "keep it"
    # note. Results/scoring.py writes one row per scored gameweek, so keeping
    # its rows while emptying gw_scores would leave a real bank and team value
    # displayed beside 0 points for gameweeks that no longer have a score.
    # Confirmed safe in Phase 1: GET /team for a user with no gw_scores and no
    # finance row returns HTTP 200 with has_lineup=false, bank=0.0,
    # team_value=0.0, gw_points=0, overall_rank=null -- it degrades to zeroes
    # rather than erroring or showing a stale bank. (It does report
    # team_value_available=true alongside team_value=0.0, which is cosmetic,
    # not a crash; noted for Phase 2.)
    #
    # league_h2h_fixtures is deliberately NOT in the TRUNCATE list -- see the
    # targeted UPDATE below. It does not hold only derived results:
    # _generate_and_insert_h2h_schedule writes the season's PAIRINGS upfront
    # and regeneration is gated on "no rows for this league+season", so
    # truncating would reshuffle who plays whom.
    op.execute("""
        TRUNCATE TABLE
            leaderboard_snapshots,
            gw_scores,
            user_gameweek_finance,
            starting_xi,
            gw_selections,
            chips,
            cancelled_transfers,
            transfers
        RESTART IDENTITY
    """)
    op.execute("UPDATE league_members SET season_points = 0, rank = 0, last_gw_points = 0")

    # league_h2h_fixtures: reset ONLY the three derived columns. The pairing
    # columns (league_id, season, gameweek, user_id_1, user_id_2) are the
    # season's schedule and are never touched, so who plays whom is unchanged.
    #
    # Each of the three is recomputed from scratch by
    # Results/standings.py::_process_h2h_league, which reads every fixture for
    # the gameweek (THIS_GW_FIXTURES_QUERY) without skipping resolved ones and
    # overwrites all three via RESOLVE_FIXTURE_STMT:
    #   points_1 / points_2 <- gw_scores.total_points, absent row counted as 0
    #   result              <- comparing those two points, or the literal
    #                          'bye' when user_id_2 IS NULL
    # So none of the three carries information that is not reconstructible
    # from the pairing plus gw_scores, and gw_scores is being emptied above --
    # leaving these set would state match results for scores that no longer
    # exist.
    #
    # 'bye' specifically survives the reset: both the writer
    # (standings.py "if fx.user_id_2 is None: result = 'bye'") and the reader
    # (Results/leagues.py "is_bye = row.user_id_2 is None") derive it from the
    # pairing column, never from this column.
    #
    # The resulting state is one the system already handles: it is exactly the
    # state every not-yet-played round is in today, and GET /leagues/{id}/h2h
    # renders it as an unplayed fixture (points null, result null), while the
    # standings queries exclude it via `result IS NOT NULL`.
    op.execute("""
        UPDATE league_h2h_fixtures
           SET points_1 = NULL,
               points_2 = NULL,
               result   = NULL
         WHERE points_1 IS NOT NULL
            OR points_2 IS NOT NULL
            OR result   IS NOT NULL
    """)
    # user_squads.total_transfers no longer exists (dropped by revision d8f4a2c60b19),
    # so there is no squad counter to reset here.

    # ------------------------------------------------------------------
    # 2. Chips are removed from the game.
    # ------------------------------------------------------------------
    op.execute("DROP TRIGGER IF EXISTS enforce_chip_limit ON chips")
    op.execute("DROP FUNCTION IF EXISTS enforce_chip_limit_fn()")
    op.execute("DROP TABLE IF EXISTS chips")

    # ------------------------------------------------------------------
    # 3. Captaincy is replaced by tactic + Bonus Players (next revision).
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE gw_selections
            DROP COLUMN IF EXISTS captain_id,
            DROP COLUMN IF EXISTS vice_captain_id,
            DROP COLUMN IF EXISTS chip_used
    """)
    op.execute("""
        ALTER TABLE starting_xi
            DROP COLUMN IF EXISTS is_captain,
            DROP COLUMN IF EXISTS is_vice_captain
    """)

    # ------------------------------------------------------------------
    # 4. Paid transfers are deferred, so hits are gone.
    #    transfers.is_free is left in place (always TRUE from now on).
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE gw_scores
            DROP COLUMN IF EXISTS transfer_hits,
            DROP COLUMN IF EXISTS hit_deductions
    """)


def downgrade() -> None:
    """Restores the v1.0 table shapes only. Wiped data is NOT restored, and the
    live chip trigger (per half-season) is not reproduced: this recreates the
    v1.0 per-season version."""
    op.execute("""
        ALTER TABLE gw_scores
            ADD COLUMN IF NOT EXISTS transfer_hits  SMALLINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS hit_deductions SMALLINT NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE starting_xi
            ADD COLUMN IF NOT EXISTS is_captain      BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS is_vice_captain BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE gw_selections
            ADD COLUMN IF NOT EXISTS captain_id      INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS vice_captain_id INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS chip_used       VARCHAR(20)
                CHECK (chip_used IN ('wildcard','bench_boost','triple_captain','free_hit'))
    """)
    op.execute("ALTER TABLE gw_selections ALTER COLUMN captain_id DROP DEFAULT")
    op.execute("ALTER TABLE gw_selections ALTER COLUMN vice_captain_id DROP DEFAULT")

    op.execute("""
        CREATE TABLE IF NOT EXISTS chips (
            id            SERIAL        PRIMARY KEY,
            user_id       INTEGER       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            season        VARCHAR(9)    NOT NULL,
            chip_type     VARCHAR(20)   NOT NULL
                CHECK (chip_type IN ('wildcard','bench_boost','triple_captain','free_hit')),
            gameweek_used SMALLINT      NOT NULL,
            used_at       TIMESTAMPTZ   DEFAULT NOW(),
            CONSTRAINT uq_chips_user_season_type UNIQUE (user_id, season, chip_type)
        )
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION enforce_chip_limit_fn()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.chip_type IN ('bench_boost', 'triple_captain', 'free_hit') THEN
                IF EXISTS (
                    SELECT 1 FROM chips
                    WHERE user_id = NEW.user_id
                      AND season = NEW.season
                      AND chip_type = NEW.chip_type
                ) THEN
                    RAISE EXCEPTION 'Chip already used this season: user_id=%, chip=%',
                        NEW.user_id, NEW.chip_type;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER enforce_chip_limit
            BEFORE INSERT ON chips
            FOR EACH ROW EXECUTE FUNCTION enforce_chip_limit_fn()
    """)
