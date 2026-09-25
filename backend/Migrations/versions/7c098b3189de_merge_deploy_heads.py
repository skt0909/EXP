"""merge deploy heads

Revision ID: 7c098b3189de
Revises: e6a4b9d2c815, fe1565dafd1a
Create Date: 2026-09-25 21:09:35.752861

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c098b3189de'
down_revision: Union[str, Sequence[str], None] = ('e6a4b9d2c815', 'fe1565dafd1a')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
