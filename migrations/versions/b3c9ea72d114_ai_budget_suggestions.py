"""Add AI budget suggestions

Revision ID: b3c9ea72d114
Revises: 8f2d1a7c9b61
Create Date: 2026-05-04 03:05:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b3c9ea72d114'
down_revision = '8f2d1a7c9b61'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if 'budget_suggestion' in inspector.get_table_names():
        return

    op.create_table(
        'budget_suggestion',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('family_id', sa.Integer(), nullable=False),
        sa.Column('created_by_user_id', sa.Integer(), nullable=False),
        sa.Column('source_start_date', sa.Date(), nullable=False),
        sa.Column('source_end_date', sa.Date(), nullable=False),
        sa.Column('target_start_date', sa.Date(), nullable=False),
        sa.Column('target_end_date', sa.Date(), nullable=False),
        sa.Column('title', sa.String(length=160), nullable=False),
        sa.Column('suggested_monthly_budget', sa.Float(), nullable=True),
        sa.Column('total_planned', sa.Float(), nullable=False),
        sa.Column('risk_level', sa.String(length=20), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('raw_json', sa.Text(), nullable=False),
        sa.Column('is_applied', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id']),
        sa.ForeignKeyConstraint(['family_id'], ['family.id']),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if 'budget_suggestion' in inspector.get_table_names():
        op.drop_table('budget_suggestion')
