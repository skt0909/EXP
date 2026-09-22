"""Add gameweeks: when a gameweek was finished being scored.

Revision ID: b4e1f37c920d
Revises: f2b9c05e7a41
Create Date: 2026-09-22

WHY THIS TABLE EXISTS. Nothing in this schema recorded "this gameweek is
done". Every existing signal is an inference:

  * ml.fixtures.finished  -- the matches are over, not that they were scored;
  * a gw_scores row       -- the job ran once, not that it will not run again
                             (it re-runs every 15 minutes for five days);
  * the 5-day active window -- "probably final" by elapsed time, not by state.

scored_at is the first state-based answer, and GET /gameweeks/current is
rewritten on top of it: the current gameweek becomes the earliest one NOT yet
scored, rather than the next one with a future deadline. Without this column
that rule cannot be expressed.

SET BY Results/scoring_job.py, under TWO conditions, both required: the batch
run completed AND every fixture in the gameweek has finished = TRUE. Scores
written mid-play are deliberately left unmarked, because the next run will
change them.

NULLABLE on purpose. A row may exist with scored_at NULL, meaning "known
about, not finished". That is a different statement from having no row, and
the rewritten current-gameweek query relies on both reading as "not scored".

NO FOREIGN KEYS. Seasons and gameweeks are not entities here -- they live as
(season, gameweek) pairs across ml.fixtures, gw_selections and gw_scores with
no table to point at. Adding one would invent a parent this schema does not
have, and would make this table deletable by cascade, which is exactly the
property ruleset_epochs was designed to avoid.
"""
from alembic import op

revision = "b4e1f37c920d"
down_revision = "f2b9c05e7a41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gameweeks (
            season    VARCHAR(9)  NOT NULL,
            gameweek  SMALLINT    NOT NULL,
            scored_at TIMESTAMPTZ,
            PRIMARY KEY (season, gameweek)
        )
        """
    )


def downgrade() -> None:
    """Drops the table. scored_at is DATA and is not recoverable: it records
    when each gameweek first completed, which cannot be reconstructed after
    the fact -- re-running the scorer would stamp today's timestamp on every
    gameweek that happens to have all its fixtures finished."""
    op.execute("DROP TABLE IF EXISTS gameweeks")
