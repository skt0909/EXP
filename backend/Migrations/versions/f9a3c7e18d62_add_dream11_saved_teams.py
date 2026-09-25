"""add dream11 saved teams

Revision ID: f9a3c7e18d62
Revises: c2f6a83e91d4
Create Date: 2026-09-24 00:00:00.000000

Lets a user build an 11-player Dream11 lineup once and reuse it, instead
of re-picking from scratch every time they join another contest on the
SAME fixture. Deliberately scoped to fixture_id, not to any one contest:
Game_logic/dream11.py's whole player pool (POOL_QUERY) is drawn from a
fixture's two clubs, so a saved lineup is meaningful for any contest on
that fixture and meaningless for any other -- there is no such thing as
a fixture-independent Dream11 team, since the eligible 22-ish players
themselves are fixture-specific.

WHY A SEPARATE PAIR OF TABLES, not e.g. a dream11.teams row with a NULL
contest_id. dream11.teams/team_players carry contest-specific state this
concept has no use for (submitted_at as a real submission timestamp,
final_points/final_minutes frozen at finalization, the
enforce_contest_lock_fn trigger gating INSERT on a contest's own
is_locked) -- overloading that table with a "template, not a real entry"
row would mean every one of those had to grow a special case for "this
one isn't real". A saved team is never scored, never locked, never
finalized; it is pure input to the picker UI, nothing else.

NO credit_price ANYWHERE HERE, unlike dream11.team_players. Prices are
frozen per-CONTEST at creation time (dream11.player_prices), so the same
11 players can cost different amounts in two different contests on the
same fixture if contest B was created later with fresher rolling-average
data. A saved team is just player identities + the captain/vice picks;
budget validation happens where it always has, at the moment of actually
submitting to a specific contest (Game_logic/dream11.py's _validate_team).

NO immutability trigger: a saved team is the user's own scratch space,
freely renameable/deletable/overwritable, the opposite of the frozen
contest-result rows b3f7c1e08d52 protects.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f9a3c7e18d62"
down_revision: Union[str, Sequence[str], None] = "c2f6a83e91d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
CREATE TABLE dream11.saved_teams (
    id serial PRIMARY KEY,
    user_id integer NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    fixture_id integer NOT NULL REFERENCES ml.fixtures(id) ON DELETE CASCADE,
    name character varying(60) NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE dream11.saved_team_players (
    id serial PRIMARY KEY,
    saved_team_id integer NOT NULL REFERENCES dream11.saved_teams(id) ON DELETE CASCADE,
    player_id integer NOT NULL REFERENCES ml.players(id) ON DELETE CASCADE,
    is_captain boolean NOT NULL DEFAULT false,
    is_vice_captain boolean NOT NULL DEFAULT false
);

-- The "how many teams have I made for this fixture" list (and the picker
-- that lets you jump straight to one when joining another contest on it)
-- is always scoped to (user_id, fixture_id) together.
CREATE INDEX idx_d11_saved_teams_user_fixture ON dream11.saved_teams (user_id, fixture_id);
CREATE INDEX idx_d11_saved_team_players_team ON dream11.saved_team_players (saved_team_id);
"""

_DOWNGRADE_SQL = """
DROP TABLE IF EXISTS dream11.saved_team_players;
DROP TABLE IF EXISTS dream11.saved_teams;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
