"""add password_reset_tokens for forgot/reset password

Revision ID: a24438b71445
Revises: 0f3a2c1d9b80
Create Date: 2026-09-12 00:00:00.000000

WHY token_hash IS sha256, NOT bcrypt (the algorithm every other hash column
in this schema uses -- see users.password_hash). A login lookup finds the
user by email FIRST, then bcrypt-*verifies* the one candidate hash it
already has. A reset-token lookup has no such anchor: the raw token in the
request body is the only thing to look up by, and bcrypt's random salt
makes "WHERE token_hash = bcrypt(raw_token)" impossible -- hashing the same
input twice gives a different digest each time. The token itself already
carries 256 bits of entropy from secrets.token_urlsafe(32) (Data/auth.py),
so a fast deterministic hash is the correct, standard choice here -- the
same reasoning Django's PasswordResetTokenGenerator and Rails' has_secure_token
use. Bcrypt's slow, salted design solves a different problem (weak, guessable
passwords) that doesn't apply to a high-entropy random token.

user_id cascades on delete: a hard-deleted password_reset_tokens row for a
soft-deleted user would otherwise sit there forever, unlike users itself
which is never actually removed (see migration a1f4e9c07b32's note on why
account deletion is soft). Nothing reads this table by anything but
token_hash or user_id, hence the two indexes and no others.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a24438b71445"
down_revision: Union[str, Sequence[str], None] = "0f3a2c1d9b80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
CREATE TABLE public.password_reset_tokens (
    id serial PRIMARY KEY,
    user_id integer NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);

CREATE INDEX idx_password_reset_tokens_token_hash ON public.password_reset_tokens(token_hash);
CREATE INDEX idx_password_reset_tokens_user_id ON public.password_reset_tokens(user_id);
"""

_DOWNGRADE_SQL = """
DROP TABLE IF EXISTS public.password_reset_tokens;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
