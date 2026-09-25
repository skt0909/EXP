"""lock dream11 teams at kickoff, in the database, for edits too

Revision ID: e6a4b9d2c815
Revises: d8c2e5a1f374
Create Date: 2026-09-25 14:00:00.000000

enforce_contest_lock_fn fired only BEFORE INSERT ON dream11.teams, and only
on contests.is_locked. That left two ways to change a team after kickoff:

  * Editing. edit_team (Game_logic/dream11.py) never inserts into
    dream11.teams; it replaces dream11.team_players rows, which no trigger
    covered. Only an is_locked check in the handler stood in the way.
  * The lock sweep's lag. is_locked is set by lock_dream11_contests every
    5 minutes, so for up to 5 minutes after kickoff both submitting and
    editing still went through.

The function now also refuses once the contest's fixture has kicked off
(the DB clock decides, as everywhere else in the lock path), and the
trigger now covers:

  * dream11.teams: INSERT, and UPDATE of the columns that make up the entry
    (contest_id, user_id, entry_name);
  * dream11.team_players: INSERT, and UPDATE of the pick itself (team_id,
    player_id, is_captain, is_vice_captain).

Deliberately NOT covered:
  * UPDATE of team_players.final_points / final_minutes -- scoring writes
    those after kickoff (Game_logic/dream11_scoring.py);
  * DELETE -- cascades from deleting a contest, fixture or user must keep
    working. An edit is delete-then-insert in one transaction, so the
    blocked insert rolls the delete back with it.

The error text is unchanged, so every handler's LOCK_ERROR_SUBSTRING
("Contest is locked") match still recognises it.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e6a4b9d2c815"
down_revision: Union[str, Sequence[str], None] = "d8c2e5a1f374"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
CREATE OR REPLACE FUNCTION dream11.enforce_contest_lock_fn() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_contest_id INTEGER;
    v_locked BOOLEAN;
    v_kickoff TIMESTAMPTZ;
BEGIN
    IF TG_TABLE_NAME = 'teams' THEN
        v_contest_id := NEW.contest_id;
    ELSE
        SELECT t.contest_id INTO v_contest_id FROM dream11.teams t WHERE t.id = NEW.team_id;
    END IF;

    SELECT c.is_locked, f.kickoff_time INTO v_locked, v_kickoff
    FROM dream11.contests c
    JOIN ml.fixtures f ON f.id = c.fixture_id
    WHERE c.id = v_contest_id;

    IF v_locked OR (v_kickoff IS NOT NULL AND v_kickoff <= now()) THEN
        RAISE EXCEPTION 'Contest is locked (fixture has kicked off): contest_id=%', v_contest_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS enforce_contest_lock ON dream11.teams;
CREATE TRIGGER enforce_contest_lock
    BEFORE INSERT OR UPDATE OF contest_id, user_id, entry_name ON dream11.teams
    FOR EACH ROW EXECUTE FUNCTION dream11.enforce_contest_lock_fn();

CREATE TRIGGER enforce_contest_lock
    BEFORE INSERT OR UPDATE OF team_id, player_id, is_captain, is_vice_captain ON dream11.team_players
    FOR EACH ROW EXECUTE FUNCTION dream11.enforce_contest_lock_fn();
"""

_DOWNGRADE_SQL = """
DROP TRIGGER IF EXISTS enforce_contest_lock ON dream11.team_players;
DROP TRIGGER IF EXISTS enforce_contest_lock ON dream11.teams;

CREATE OR REPLACE FUNCTION dream11.enforce_contest_lock_fn() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_locked BOOLEAN;
BEGIN
    SELECT is_locked INTO v_locked FROM dream11.contests WHERE id = NEW.contest_id;
    IF v_locked THEN
        RAISE EXCEPTION 'Contest is locked (fixture has kicked off): contest_id=%', NEW.contest_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER enforce_contest_lock
    BEFORE INSERT ON dream11.teams
    FOR EACH ROW EXECUTE FUNCTION dream11.enforce_contest_lock_fn();
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
