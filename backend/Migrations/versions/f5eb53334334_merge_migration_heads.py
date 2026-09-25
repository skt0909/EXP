"""merge migration heads

Revision ID: f5eb53334334
Revises: a24438b71445, c2f6a83e91d4
Create Date: 2026-09-14 10:18:04.267891

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5eb53334334'
down_revision: Union[str, Sequence[str], None] = ('a24438b71445', 'c2f6a83e91d4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
