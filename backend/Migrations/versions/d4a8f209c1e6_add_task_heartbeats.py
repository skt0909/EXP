"""add task heartbeats

Revision ID: d4a8f209c1e6
Revises: b3f7c1e08d52
Create Date: 2026-09-03 00:00:00.000000

The smallest addition that makes "Beat silently stopped" detectable.

WHY. Nothing in this project currently records that a Beat-scheduled task
ran. If the Beat process dies, every one of the eight tasks in
Worker/celery_app.py's beat_schedule simply stops firing, and nothing
notices: gameweeks stop locking, Dream11 contests stop finalizing,
fixtures stop refreshing -- all silently, all at once. This table exists
so a health check can ask "when did each task last actually run?" and
compare that against its own configured interval, rather than that
question having no answer at all.

This is deliberately NOT a monitoring or alerting system -- there is no
poller, no notification, no dashboard here. It is one table two columns
wider than the minimum, written to by the tasks that already run, and
read by GET /health/scheduled-tasks (Context_assembler/main.py). Turning
"is this stale" into a page or an email is the real Observability
project's job, planned separately; this just makes the underlying fact
queryable, which that project cannot be built on top of otherwise.

WHY TWO TIMESTAMPS, NOT ONE. A single last_run_at cannot distinguish
"Beat is down" from "Beat is fine but this task keeps failing" -- both
would go equally stale under a naive "last run" column, and those are
different incidents with different fixes. last_success_at and
last_failure_at are updated independently and never clear each other: a
task succeeding now does not erase when it last failed, and vice versa.
So "last_success_at is 3 days old" (Beat is down, or newly deployed and
never scheduled) reads differently from "last_success_at is 30 seconds
ago, last_failure_at is 2 minutes ago" (running on schedule, currently
flaky). last_error carries the exception text from the most recent
failure for the second case.

WHY public, NOT ml OR dream11. This is cross-cutting operational state
about the scheduler, not game data -- it says nothing about any player,
fixture or contest. public already holds the project's other
operational-not-gameplay tables (chips, gw_scores), so it fits there
rather than forcing a choice between the two gameplay schemas it has
nothing to do with.

No index beyond the primary key: task_name has eight possible values
today and the table is read in full by the health endpoint every time,
never filtered.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d4a8f209c1e6"
down_revision: Union[str, Sequence[str], None] = "b3f7c1e08d52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
CREATE TABLE public.task_heartbeats (
    task_name text PRIMARY KEY,
    last_success_at timestamp with time zone,
    last_failure_at timestamp with time zone,
    last_error text
);
"""

_DOWNGRADE_SQL = """
DROP TABLE IF EXISTS public.task_heartbeats;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
