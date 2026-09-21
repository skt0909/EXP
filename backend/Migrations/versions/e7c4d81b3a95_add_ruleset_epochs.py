"""Add ruleset_epochs: the stored first Gameweek of a ruleset.

Revision ID: e7c4d81b3a95
Revises: d58b3f10a7c2
Create Date: 2026-09-21

WHY THIS TABLE EXISTS. The tactical free-transfer recurrence needs an anchor:
the first Gameweek played under the new rules, from which every manager gets 1
free transfer and then banks to the cap. The obvious anchor -- "the earliest
Gameweek that has a gw_selections row" -- was rejected after checking the live
schema:

    enforce_selection_lock :: BEFORE UPDATE ON gw_selections   (no DELETE)
    DELETE triggers on gw_selections: 0
    gw_selections_user_id_fkey :: REFERENCES users(id) ON DELETE CASCADE

Those rows are freely deletable and cascade from users, so deleting one account
could move the anchor forward and SILENTLY restate every manager's allowance --
the allowance is derived on every request, so there is no stored value to
compare against. This table is the fix: written once at release, append-only,
and with NO foreign keys, so nothing cascades into it and nothing can move it.

PER DATABASE. The epoch is computed from the fixtures present when THIS
database is migrated, so dev, test and the server each get their own. That is
deliberate: they are released at different moments, and an epoch copied between
them would be wrong for all but one.
"""
import sqlalchemy as sa
from alembic import op

revision = "e7c4d81b3a95"
down_revision = "d58b3f10a7c2"
branch_labels = None
depends_on = None

# Must match Shared/rules.py's RULES_VERSION. Restated as a literal rather than
# imported: a migration is a historical record and must keep applying the same
# way after the constant moves on.
RULES_VERSION = 3

# The deadline, exactly as Shared/deadlines.py defines it:
#   _DEADLINE_EXPR = f"MIN(kickoff_time) - interval '{DEADLINE_OFFSET_MINUTES} minutes'"
#   DEADLINE_OFFSET_MINUTES = 90
# Restated here for the same reason as above, and kept character-identical so a
# reader can diff the two by eye.
DEADLINE_OFFSET_MINUTES = 90


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ruleset_epochs (
            season        VARCHAR(9)  NOT NULL,
            rules_version SMALLINT    NOT NULL,
            first_gameweek SMALLINT   NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (season, rules_version)
        )
        """
    )

    # Append-only, following the pattern transfers already uses
    # (enforce_transfers_immutability_fn). An epoch that could be edited would
    # be exactly as dangerous as the derived anchor this table replaces.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_ruleset_epochs_immutability_fn()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'ruleset_epochs is append-only: % on (season=%, rules_version=%) is not allowed. '
                'The first gameweek of a ruleset is a historical fact -- correcting it means '
                'inserting a new rules_version, not rewriting this one.',
                TG_OP,
                COALESCE(OLD.season, NEW.season),
                COALESCE(OLD.rules_version, NEW.rules_version);
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER enforce_ruleset_epochs_immutability
            BEFORE UPDATE OR DELETE ON ruleset_epochs
            FOR EACH ROW EXECUTE FUNCTION enforce_ruleset_epochs_immutability_fn()
        """
    )

    _insert_epoch_for_this_database()


def _insert_epoch_for_this_database() -> None:
    """Compute and store this database's epoch, per rule D5.

    CURRENT SEASON. The application's own current_live_season()
    (Data/fpl_ingest.py:187) asks the FPL API, which a migration must not do --
    there is no network in CI and a schema change must not depend on a third
    party being up. The database-side equivalent used here is the season
    holding the next kickoff still to come, restricted to real season codes:
    the `season ~ '^[0-9]{4}-[0-9]{2}$'` filter excludes the SIM38OK / SIM38TST
    / SIMSMOKE simulation seasons the plan says never to treat as game data.
    Discrepancy recorded in PHASE3C_REPORT.md rather than papered over.

    FIRST GAMEWEEK. Rule D5: the first Gameweek whose deadline is still in the
    future at migration time. Migrate inside the release window and this is the
    Gameweek managers are about to pick for.

    NOTHING TO DO is a normal outcome, not a failure: a fresh CI database has
    no fixtures, and between seasons there is no future kickoff. Both insert
    nothing and say so. A season with no epoch row is read as "these rules
    applied from Gameweek 1", which is the right answer for a database that has
    never known anything else.
    """
    conn = op.get_bind()

    season = conn.execute(
        sa.text(
            """
            SELECT season FROM ml.fixtures
            WHERE kickoff_time > now()
              AND season ~ '^[0-9]{4}-[0-9]{2}$'
            ORDER BY kickoff_time
            LIMIT 1
            """
        )
    ).scalar()

    if season is None:
        print(
            "NOTICE: ruleset_epochs -- no future fixture in any real season, so no "
            "current season to anchor. Inserting nothing; this season will be read "
            "as starting under these rules at gameweek 1. Expected on a fresh CI "
            "database and between seasons."
        )
        return

    first_gameweek = conn.execute(
        sa.text(
            f"""
            SELECT gameweek FROM ml.fixtures
            WHERE season = :season
            GROUP BY gameweek
            HAVING MIN(kickoff_time) - interval '{DEADLINE_OFFSET_MINUTES} minutes' > now()
            ORDER BY gameweek
            LIMIT 1
            """
        ),
        {"season": season},
    ).scalar()

    if first_gameweek is None:
        print(
            f"NOTICE: ruleset_epochs -- season {season} has no gameweek whose deadline "
            f"is still ahead (the season is over, or every deadline has passed). "
            f"Inserting nothing."
        )
        return

    # ON CONFLICT DO NOTHING: re-running must never duplicate or silently
    # rewrite an epoch that is already the historical record.
    conn.execute(
        sa.text(
            """
            INSERT INTO ruleset_epochs (season, rules_version, first_gameweek)
            VALUES (:season, :rules_version, :first_gameweek)
            ON CONFLICT (season, rules_version) DO NOTHING
            """
        ),
        {"season": season, "rules_version": RULES_VERSION, "first_gameweek": first_gameweek},
    )

    stored = conn.execute(
        sa.text(
            "SELECT season, rules_version, first_gameweek, created_at FROM ruleset_epochs "
            "WHERE season = :season AND rules_version = :rules_version"
        ),
        {"season": season, "rules_version": RULES_VERSION},
    ).first()

    print(
        f"NOTICE: ruleset_epochs -- computed season={season!r} "
        f"rules_version={RULES_VERSION} first_gameweek={first_gameweek}. "
        f"Stored row: season={stored.season!r} rules_version={stored.rules_version} "
        f"first_gameweek={stored.first_gameweek} created_at={stored.created_at}"
        + ("" if stored.first_gameweek == first_gameweek else
           "  <-- AN EPOCH ALREADY EXISTED AND WAS KEPT; the computed value was NOT stored")
    )


def downgrade() -> None:
    """Drops the table and its trigger. The stored epoch is DATA and is not
    recoverable from the schema -- re-upgrading recomputes it from whatever
    fixtures exist at that later moment, which will not be the same answer.
    Downgrading past this revision after a release therefore loses the record
    of when the ruleset began."""
    op.execute("DROP TRIGGER IF EXISTS enforce_ruleset_epochs_immutability ON ruleset_epochs")
    op.execute("DROP FUNCTION IF EXISTS enforce_ruleset_epochs_immutability_fn()")
    op.execute("DROP TABLE IF EXISTS ruleset_epochs")
