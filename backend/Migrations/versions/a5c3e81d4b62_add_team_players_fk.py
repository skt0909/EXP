"""add the missing FK on dream11.team_players.player_id

Revision ID: a5c3e81d4b62
Revises: f6b8d2c91a44
Create Date: 2026-08-29 00:00:00.000000

dream11.team_players.player_id was a bare integer with no foreign key,
while its sibling dream11.player_prices.player_id has always had one to
ml.players(id) ON DELETE CASCADE. Both are joined to ml.players.id in
every query that reads them (Game_logic/dream11.py's USER_TEAM_QUERY,
Game_logic/dream11_scoring.py's TEAM_STATS_QUERY), so the reference was
real but unenforced -- an omission rather than a decision.

WHY IT MATTERED, verified rather than assumed. The application path
could not write a bad id: submit_team rejects any fpl_id that does not
resolve in ml.players and then filters to the contest's priced pool, so
only real, in-pool players are ever inserted. The reachable hole was
DELETION. Removing an ml.players row cascaded player_prices away and
left team_players pointing at nothing -- demonstrated directly against
the test database (inside a rolled-back transaction) before writing
this. backend/Tests/conftest.py performs exactly that delete, for
TEST_SEASON, on every single test.

Both databases were checked for existing orphans before this ran:
dev had 11 team_players rows and 0 orphans, test had 0 rows and 0
orphans, so this constraint applies to clean data and deletes nothing.

ON DELETE CASCADE, matching player_prices rather than RESTRICT: the
whole dream11 tree already cascades from ml data -- contests cascade
from ml.fixtures, teams from contests, player_prices from ml.players --
so a team_players row outliving its player would be the anomaly, not the
cascade.

PLAYER ID CONVENTION, stated here so the schema records it too. This FK
points at ml.players.id, the INTERNAL SERIAL. That is the Dream11
convention for both of its player-referencing tables, and it is the
OPPOSITE of the classic FPL tables (squad_players.player_id,
starting_xi.player_id, transfers.player_in_id/player_out_id), which all
store the raw fpl_id and never translate. Dream11's HTTP API still
accepts and returns fpl_ids; the single conversion point is
PLAYERS_LOOKUP_QUERY in Game_logic/dream11.py, and that module's
docstring is the fuller explanation. Unifying the two conventions is an
open decision, deliberately NOT taken here.

Hand-authored, same as every prior revision -- this project has no
SQLAlchemy ORM models to autogenerate from.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a5c3e81d4b62'
down_revision: Union[str, Sequence[str], None] = 'f6b8d2c91a44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
ALTER TABLE dream11.team_players
    ADD CONSTRAINT team_players_player_id_fkey
    FOREIGN KEY (player_id) REFERENCES ml.players(id) ON DELETE CASCADE;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "ALTER TABLE dream11.team_players DROP CONSTRAINT IF EXISTS team_players_player_id_fkey"
    )
