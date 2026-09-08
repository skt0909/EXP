"""add gw_scores.rules_version

Revision ID: c9a04e7b53d1
Revises: d8f4a2c60b19
Create Date: 2026-08-30 00:00:00.000000

Records which generation of the scoring rules produced each gw_scores
row, so a gameweek scored before a rules change can be told apart from
one scored after it without anyone having to remember when the change
shipped.

This exists because rescoring history was investigated and rejected. The
FPL-2026-27 rules migration (f6b8d2c91a44) changed how points are
computed, but the inputs the new formula needs -- notably
defensive_contributions -- were never ingested for 2025-26 and cannot be
now: those stat rows are settled and frozen by
enforce_gw_stats_immutability. Rescoring that season would not restore
correctness, it would produce a differently wrong number. Freezing the
past under the rules that produced it is the only honest option
available, and this column is what makes that state legible rather than
accidental.

TYPE. smallint, monotonic, deliberately NOT a label or a semver string.
The point is that code can ask "was this scored before version N?" with
an ordinary comparison; a free-text tag would only be displayable.

NO DEFAULT, on purpose. The column is added WITH a default of 1 so the
existing rows backfill in one statement, then the default is dropped
immediately. Every future writer must therefore state a version
explicitly. A lingering DEFAULT 1 would mean a new writer that forgot to
stamp would silently label fresh rows as pre-2026 -- wrong in a way that
looks plausible, which is worse than failing.

BACKFILL. All existing rows become version 1, "the original wholesale
total_points era", which predates every rules change made since:
component scoring, free-transfer banking, half-season chips, the
half-profit sell price, and defensive contributions. That is correct for
the single real historical row (2025-26 GW1, scored long before any of
them). Test-season rows (9999-00) get 1 as well -- the value is
meaningless there since those rows are wiped and rewritten constantly,
and giving them the same value keeps this a single statement rather than
a special case.

NOT ADDRESSED HERE, deliberately: gw_scores.season_total still sums
prior total_points regardless of version, so a rules change mid-season
would produce a season total spanning two rule sets. That is a product
question, not a schema one, and is left open -- see
backend/Tests/README.md.

Hand-authored, same as every prior revision -- this project has no
SQLAlchemy ORM models to autogenerate from.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c9a04e7b53d1'
down_revision: Union[str, Sequence[str], None] = 'd8f4a2c60b19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
ALTER TABLE public.gw_scores
    ADD COLUMN rules_version smallint NOT NULL DEFAULT 1;

-- Backfill is done; from here every writer states its own version.
ALTER TABLE public.gw_scores
    ALTER COLUMN rules_version DROP DEFAULT;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE public.gw_scores DROP COLUMN IF EXISTS rules_version")
