"""create nav_records table (repairs a gap in the migration chain)

Revision ID: a1b2c3d4e5f6
Revises: ab2c417ec849
Create Date: 2026-08-15 00:00:00.000000+00:00

WHY THIS EXISTS
---------------
nav_records was never created by any migration. It existed in the original
development database (created outside alembic, most likely by an early
create_all() or by hand), so nothing ever failed locally. But migration
4b6e461f682d ALTERS nav_records, which meant the chain could not build a
database from scratch: a fresh deploy failed with

    UndefinedTableError: relation "nav_records" does not exist

This migration is inserted BETWEEN the initial tables and the migration that
alters nav_records, so the chain is now complete and reproducible.

The table definition matches models.py exactly, including the two indexes and
the unique constraint on (product_id, period_label).

SAFE ON EXISTING DATABASES: uses checkfirst semantics via a guard, so running
this against a database that already has nav_records (such as the original
development one) does nothing rather than erroring.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'ab2c417ec849'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(sa.text(
        "SELECT to_regclass('public.nav_records')"
    )).scalar()
    if exists:
        # Already present (the original development database). Nothing to do.
        return

    op.create_table(
        'nav_records',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('product_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('period_label', sa.String(length=50), nullable=False),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('nav_per_unit', sa.Float(), nullable=True),
        sa.Column('return_pct', sa.Float(), nullable=True),
        sa.Column('cumulative_pct', sa.Float(), nullable=True),
        sa.Column('total_aum', sa.Float(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False, server_default='manual'),
        sa.Column('entered_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['entered_by'], ['admin_users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('product_id', 'period_label', name='uq_nav_product_period'),
    )
    op.create_index('ix_nav_product_id', 'nav_records', ['product_id'], unique=False)
    op.create_index('ix_nav_period_end', 'nav_records', ['period_end'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_nav_period_end', table_name='nav_records')
    op.drop_index('ix_nav_product_id', table_name='nav_records')
    op.drop_table('nav_records')
