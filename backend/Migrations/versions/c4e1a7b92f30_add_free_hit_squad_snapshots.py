"""add public.free_hit_squads snapshot table

Revision ID: c4e1a7b92f30
Revises: 1acbf07cfb53
Create Date: 2026-08-25 00:00:00.000000

Backs the Free Hit chip's squad revert, which had no storage and
therefore no implementation: `Game_logic/transfers.py` already treated
`free_hit` as an uncapped free-transfer chip (FREE_CHIPS), but nothing
anywhere restored the pre-chip squad afterwards, so a Free Hit behaved
as a permanent second Wildcard.

One row per player in the snapshot (15 for a full squad), keyed by the
gameweek the chip was played in. `budget_remaining` is denormalised onto
every row rather than split into a header table: the snapshot is written
once inside the same transaction that activates the chip and is never
updated, so there is no update anomaly to protect against, and a single
table keeps the revert a single DELETE/INSERT pass.

`reverted_at` is NULL until `Game_logic/scheduling.py`'s
revert_expired_free_hits processes it, which is what makes that function
idempotent -- rows are matched on `reverted_at IS NULL`, so re-running it
(Beat retries, a manual Tools/fpl_sim.py run) cannot revert twice.

Hand-authored, same as both prior revisions -- this project has no
SQLAlchemy ORM models to autogenerate from.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4e1a7b92f30'
down_revision: Union[str, Sequence[str], None] = '1acbf07cfb53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
CREATE SEQUENCE public.free_hit_squads_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;

CREATE TABLE public.free_hit_squads (
    id integer NOT NULL DEFAULT nextval('public.free_hit_squads_id_seq'::regclass),
    user_id integer NOT NULL,
    season character varying(10) NOT NULL,
    gameweek integer NOT NULL,
    player_id integer NOT NULL,
    purchase_price integer NOT NULL,
    budget_remaining integer NOT NULL,
    reverted_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT free_hit_squads_pkey PRIMARY KEY (id),
    CONSTRAINT free_hit_squads_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE,
    CONSTRAINT uq_free_hit_squads_user_season_gw_player UNIQUE (user_id, season, gameweek, player_id)
);

ALTER SEQUENCE public.free_hit_squads_id_seq OWNED BY public.free_hit_squads.id;

-- The revert scan's access pattern: "which snapshots are still pending?"
CREATE INDEX idx_free_hit_squads_pending ON public.free_hit_squads (season, gameweek) WHERE reverted_at IS NULL;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS public.free_hit_squads CASCADE")
