"""Add expense descriptions for AI organization

Revision ID: 3f2a9c8d7e61
Revises: d91a7f6c2b35
Create Date: 2026-05-05 23:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '3f2a9c8d7e61'
down_revision = 'd91a7f6c2b35'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {
        column['name']
        for column in inspector.get_columns('expense')
    }

    if 'description' not in columns:
        with op.batch_alter_table('expense', schema=None) as batch_op:
            batch_op.add_column(sa.Column('description', sa.Text(), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {
        column['name']
        for column in inspector.get_columns('expense')
    }

    if 'description' in columns:
        with op.batch_alter_table('expense', schema=None) as batch_op:
            batch_op.drop_column('description')
