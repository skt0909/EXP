"""add public.transfer_drafts

Revision ID: b7d2f4a91c05
Revises: c4e1a7b92f30
Create Date: 2026-08-28 00:00:00.000000

Server-side storage for the transfer "cart" that
frontend/src/pages/Transfers/TransfersPage.jsx has always kept in local
React state: a manager stages pairs, sees a running cost estimate, and
only then hits Confirm. Because that cart lived in the browser, a page
refresh or a switch to another device threw it away.

This table is ONLY the cart. Nothing in the scoring, free-transfer or
chip paths reads it -- `public.transfers` remains the single source of
truth for a real, committed transfer, unchanged and still append-only.
Confirm still writes there directly via POST /transfers. Moving the
point at which a transfer becomes permanent is deliberately NOT part of
this change.

Three constraint decisions, each deliberate:

  * UNIQUE (user_id, season, gameweek, player_out_id) -- a player can be
    drafted out at most once per gameweek, which makes "pick a different
    replacement for this player" an upsert rather than a second row plus
    a cleanup problem.

  * NO uniqueness on player_in_id. Mid-edit, a manager may legitimately
    have the same incoming target queued against two different outgoing
    players before resolving which one they actually want. Constraining
    it would reject a normal intermediate state.

  * NO immutability trigger, unlike `transfers`
    (enforce_transfers_immutability_fn). Drafts exist to be edited and
    thrown away; that is the entire point. The ON DELETE CASCADE from
    users follows from the same reasoning, and makes this the first
    transfer-shaped table a test can actually tear down -- `transfers`
    blocks even a cascade from its parent users row, which is why
    several test files carry uuid-unique users they can never clean up.

Hand-authored, same as every prior revision -- this project has no
SQLAlchemy ORM models to autogenerate from.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7d2f4a91c05'
down_revision: Union[str, Sequence[str], None] = 'c4e1a7b92f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
CREATE SEQUENCE public.transfer_drafts_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;

CREATE TABLE public.transfer_drafts (
    id integer NOT NULL DEFAULT nextval('public.transfer_drafts_id_seq'::regclass),
    user_id integer NOT NULL,
    season character varying(9) NOT NULL,
    gameweek smallint NOT NULL,
    player_out_id integer NOT NULL,
    player_in_id integer NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT transfer_drafts_pkey PRIMARY KEY (id),
    CONSTRAINT transfer_drafts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE,
    CONSTRAINT transfer_drafts_distinct_players CHECK (player_out_id <> player_in_id),
    CONSTRAINT uq_transfer_drafts_user_season_gw_out UNIQUE (user_id, season, gameweek, player_out_id)
);

ALTER SEQUENCE public.transfer_drafts_id_seq OWNED BY public.transfer_drafts.id;

-- The only read pattern: "what is in this manager's cart for this gameweek?"
CREATE INDEX idx_transfer_drafts_user_season_gw
    ON public.transfer_drafts (user_id, season, gameweek);
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS public.transfer_drafts CASCADE")
    op.execute("DROP SEQUENCE IF EXISTS public.transfer_drafts_id_seq")
