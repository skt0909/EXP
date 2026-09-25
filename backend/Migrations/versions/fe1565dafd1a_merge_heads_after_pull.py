"""merge heads after pull

Revision ID: fe1565dafd1a
Revises: f5eb53334334, a06f58d93f5a
Create Date: 2026-09-20 12:36:02.948419

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fe1565dafd1a'
down_revision: Union[str, Sequence[str], None] = ('f5eb53334334', 'a06f58d93f5a')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
