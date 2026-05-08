"""Add daily budget reports

Revision ID: a6d4f2b8c901
Revises: f7a2c9d4e5b6
Create Date: 2026-05-08 21:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a6d4f2b8c901'
down_revision = 'f7a2c9d4e5b6'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if 'budget_report' in inspector.get_table_names():
        return

    op.create_table(
        'budget_report',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('family_id', sa.Integer(), nullable=False),
        sa.Column('report_date', sa.Date(), nullable=False),
        sa.Column('total_spent_today', sa.Float(), nullable=False, server_default='0'),
        sa.Column('total_spent_this_week', sa.Float(), nullable=False, server_default='0'),
        sa.Column('total_spent_this_month', sa.Float(), nullable=False, server_default='0'),
        sa.Column('monthly_budget', sa.Float(), nullable=False, server_default='0'),
        sa.Column('budget_remaining', sa.Float(), nullable=False, server_default='0'),
        sa.Column('budget_used_percentage', sa.Float(), nullable=False, server_default='0'),
        sa.Column('top_spending_category', sa.String(length=100), nullable=True),
        sa.Column('top_spending_amount', sa.Float(), nullable=False, server_default='0'),
        sa.Column('spending_trend', sa.String(length=20), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('suggestions', sa.Text(), nullable=True),
        sa.Column('warnings', sa.Text(), nullable=True),
        sa.Column('insights', sa.Text(), nullable=True),
        sa.Column('category_breakdown', sa.Text(), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['family_id'], ['family.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_budget_report_report_date'), 'budget_report', ['report_date'], unique=False)


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if 'budget_report' in inspector.get_table_names():
        op.drop_index(op.f('ix_budget_report_report_date'), table_name='budget_report')
        op.drop_table('budget_report')
