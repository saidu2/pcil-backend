"""add token purpose

Revision ID: 6a683be7bf1b
Revises: b398ce9c63e6
Create Date: 2026-08-15 00:00:00.000000+00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '6a683be7bf1b'
down_revision: Union[str, None] = 'b398ce9c63e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default is needed in case any tokens were already created during
    # password reset testing. 'password_reset' is the correct value for those,
    # since email verification did not exist when they were issued.
    op.add_column(
        'password_reset_tokens',
        sa.Column('purpose', sa.String(length=30), nullable=False, server_default='password_reset')
    )


def downgrade() -> None:
    op.drop_column('password_reset_tokens', 'purpose')
