"""add frozen per-player scoring breakdown snapshots

Revision ID: c4d9a2e7f1b6
Revises: b7e3f1a9c2d4
Create Date: 2026-09-29 18:00:00.000000

A player's per-category scoring breakdown was computed on every read and
never stored, so it was lost wherever a result is frozen:

  * dream11.team_players.final_breakdown -- Quick 11. The itemized
    dream11_points_breakdown() dict, written by the same scoring UPDATE as
    final_points / final_minutes (Game_logic/dream11_scoring.py), so it
    follows exactly their lifecycle: rewritten on every pass, frozen by the
    finalizing one. A finalized contest's team read returns it instead of
    null; the read never goes back to ml.player_gw_stats.
  * public.gw_scores.player_breakdowns -- Tactic mode. The whole selection's
    per-player snapshot (Results/tactical_breakdowns.selection_snapshot),
    written in the same UPSERT as that manager's gw_scores totals, from the
    same scoring result. A settled gameweek's Dashboard reads it instead of
    re-running the engine under whatever the constants are today.

Both nullable, no default, no backfill: rows written before this keep NULL,
and their readers fall back (see those modules). Neither table's triggers
cover these columns.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c4d9a2e7f1b6"
down_revision: Union[str, Sequence[str], None] = "b7e3f1a9c2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
ALTER TABLE dream11.team_players ADD COLUMN final_breakdown jsonb;
ALTER TABLE public.gw_scores ADD COLUMN player_breakdowns jsonb;
"""

_DOWNGRADE_SQL = """
ALTER TABLE public.gw_scores DROP COLUMN IF EXISTS player_breakdowns;
ALTER TABLE dream11.team_players DROP COLUMN IF EXISTS final_breakdown;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
