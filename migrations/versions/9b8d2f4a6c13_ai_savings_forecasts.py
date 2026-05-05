"""Add AI savings forecasts

Revision ID: 9b8d2f4a6c13
Revises: 3f2a9c8d7e61
Create Date: 2026-05-06 00:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '9b8d2f4a6c13'
down_revision = '3f2a9c8d7e61'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    family_columns = {
        column['name']
        for column in inspector.get_columns('family')
    }
    if 'ai_notes' not in family_columns:
        with op.batch_alter_table('family', schema=None) as batch_op:
            batch_op.add_column(sa.Column('ai_notes', sa.Text(), nullable=True))

    if 'ai_savings_forecast' in inspector.get_table_names():
        return

    op.create_table(
        'ai_savings_forecast',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('family_id', sa.Integer(), nullable=False),
        sa.Column('created_by_user_id', sa.Integer(), nullable=False),
        sa.Column('source_start_date', sa.Date(), nullable=False),
        sa.Column('forecast_end_date', sa.Date(), nullable=False),
        sa.Column('current_spent', sa.Float(), nullable=False),
        sa.Column('predicted_additional_spend', sa.Float(), nullable=False),
        sa.Column('predicted_total_spend', sa.Float(), nullable=False),
        sa.Column('expected_savings', sa.Float(), nullable=False),
        sa.Column('confidence_level', sa.String(length=20), nullable=False),
        sa.Column('confidence_score', sa.Float(), nullable=False),
        sa.Column('admin_notes', sa.Text(), nullable=True),
        sa.Column('raw_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id']),
        sa.ForeignKeyConstraint(['family_id'], ['family.id']),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if 'ai_savings_forecast' in inspector.get_table_names():
        op.drop_table('ai_savings_forecast')

    family_columns = {
        column['name']
        for column in inspector.get_columns('family')
    }
    if 'ai_notes' in family_columns:
        with op.batch_alter_table('family', schema=None) as batch_op:
            batch_op.drop_column('ai_notes')
