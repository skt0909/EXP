"""add dream11.player_prices.rolling_points

Revision ID: c2f6a83e91d4
Revises: a1f4e9c07b32
Create Date: 2026-09-10 00:00:00.000000

Game_logic/dream11.py's create_contest already computes each pool player's
mean total_points over their last <=5 completed gameweeks
(PRICE_INPUT_QUERY's pts_rolling_5gw) to derive credit_price -- but only
credit_price was ever persisted; the rolling average itself was discarded
the moment pricing finished. That meant the pick-team screen could show a
price but never a "how have they been playing" number the way Classic
FPL's squad-selection screen already does with a settled gameweek's
points.

This column stores that same rolling average alongside the price it
produced, frozen at contest creation exactly like credit_price is (no
trigger needed beyond the existing enforce_price_immutability, which
already covers every column on an UPDATE, not just credit_price).
NULL means "no prior data" -- the same condition _compute_prices reads as
"price at the floor" -- so the two columns agree on what "unknown" means
for a given player rather than one saying 6.0 and the other saying 0.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c2f6a83e91d4"
down_revision: Union[str, Sequence[str], None] = "a1f4e9c07b32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
ALTER TABLE dream11.player_prices ADD COLUMN rolling_points double precision;
"""

_DOWNGRADE_SQL = """
ALTER TABLE dream11.player_prices DROP COLUMN IF EXISTS rolling_points;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
