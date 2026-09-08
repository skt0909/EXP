"""add public.cancelled_transfers

Revision ID: e3a91b7c2d40
Revises: b7d2f4a91c05
Create Date: 2026-08-29 00:00:00.000000

Cancelling a Free Hit before the deadline reverses the transfers made
under it -- "your previous transfer situation is restored", per the FPL
rules. Game_logic/starting_xi.py already put the SQUAD back
(scheduling.restore_free_hit_snapshot), but the free transfers those
reversed moves consumed stayed spent, because the free-transfer
allowance is derived by counting is_free = TRUE rows in `transfers`
and that table is append-only: enforce_transfers_immutability_fn raises
on every UPDATE and DELETE, unconditionally, so a row can be neither
removed nor unmarked.

Hence a side table naming the reversed rows, rather than a status column
on `transfers` that nothing could ever set. `transfers` itself stays
byte-for-byte append-only. The four places that count transfers --
transfers.py's three free-transfer queries and scoring.py's hit
deduction -- exclude anything listed here, which is what makes a
cancelled transfer stop counting against the allowance both in its own
gameweek and in the banking recurrence that follows it.

PRIMARY KEY on transfer_id rather than a surrogate id: a transfer is
either reversed or it isn't, so listing one twice is meaningless, and
the constraint makes the INSERT idempotent (ON CONFLICT DO NOTHING) if
a cancel is ever retried.

Rows here are as permanent as the `transfers` rows they point at -- the
ON DELETE CASCADE is for completeness, since nothing can delete a
transfer to trigger it.

Scope note: this covers Free Hit cancellation only. A Wildcard cannot be
cancelled once activated, and a Free Hit that is actually PLAYED is a
separate question (real FPL preserves the bank across a chip gameweek)
tracked as its own change -- see backend/Tests/README.md.

Hand-authored, same as every prior revision -- this project has no
SQLAlchemy ORM models to autogenerate from.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e3a91b7c2d40'
down_revision: Union[str, Sequence[str], None] = 'b7d2f4a91c05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
CREATE TABLE public.cancelled_transfers (
    transfer_id integer NOT NULL,
    cancelled_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT cancelled_transfers_pkey PRIMARY KEY (transfer_id),
    CONSTRAINT cancelled_transfers_transfer_id_fkey
        FOREIGN KEY (transfer_id) REFERENCES public.transfers(id) ON DELETE CASCADE
);
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS public.cancelled_transfers CASCADE")
