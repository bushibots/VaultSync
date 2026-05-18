"""Add monthly finance overrides and AI plan rollback snapshots

Revision ID: c8e1f4a2b790
Revises: a6d4f2b8c901
Create Date: 2026-05-18 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c8e1f4a2b790'
down_revision = 'a6d4f2b8c901'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'family' in tables:
        family_columns = {column['name'] for column in inspector.get_columns('family')}
        if 'monthly_income' not in family_columns:
            op.add_column('family', sa.Column('monthly_income', sa.Float(), nullable=True))
            bind.execute(sa.text('UPDATE family SET monthly_income = monthly_budget WHERE monthly_income IS NULL'))

    if 'monthly_finance_setting' not in tables:
        op.create_table(
            'monthly_finance_setting',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('family_id', sa.Integer(), nullable=False),
            sa.Column('month', sa.Integer(), nullable=False),
            sa.Column('year', sa.Integer(), nullable=False),
            sa.Column('monthly_income', sa.Float(), nullable=True),
            sa.Column('monthly_budget', sa.Float(), nullable=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('updated_by_user_id', sa.Integer(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['family_id'], ['family.id']),
            sa.ForeignKeyConstraint(['updated_by_user_id'], ['user.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_monthly_finance_setting_family_id'), 'monthly_finance_setting', ['family_id'], unique=False)

    if 'budget_plan_application' not in tables:
        op.create_table(
            'budget_plan_application',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('family_id', sa.Integer(), nullable=False),
            sa.Column('suggestion_id', sa.Integer(), nullable=False),
            sa.Column('applied_by_user_id', sa.Integer(), nullable=False),
            sa.Column('target_month', sa.Integer(), nullable=False),
            sa.Column('target_year', sa.Integer(), nullable=False),
            sa.Column('created_expected_expense_ids', sa.Text(), nullable=True),
            sa.Column('created_category_ids', sa.Text(), nullable=True),
            sa.Column('snapshot_json', sa.Text(), nullable=False),
            sa.Column('reverted_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['applied_by_user_id'], ['user.id']),
            sa.ForeignKeyConstraint(['family_id'], ['family.id']),
            sa.ForeignKeyConstraint(['suggestion_id'], ['budget_suggestion.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_budget_plan_application_family_id'), 'budget_plan_application', ['family_id'], unique=False)
        op.create_index(op.f('ix_budget_plan_application_suggestion_id'), 'budget_plan_application', ['suggestion_id'], unique=False)


def downgrade():
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()

    if 'budget_plan_application' in tables:
        op.drop_index(op.f('ix_budget_plan_application_suggestion_id'), table_name='budget_plan_application')
        op.drop_index(op.f('ix_budget_plan_application_family_id'), table_name='budget_plan_application')
        op.drop_table('budget_plan_application')

    if 'monthly_finance_setting' in tables:
        op.drop_index(op.f('ix_monthly_finance_setting_family_id'), table_name='monthly_finance_setting')
        op.drop_table('monthly_finance_setting')

    if 'family' in tables:
        family_columns = {column['name'] for column in inspector.get_columns('family')}
        if 'monthly_income' in family_columns:
            op.drop_column('family', 'monthly_income')
