"""Add category auto-color flag

Revision ID: f7a2c9d4e5b6
Revises: 9b8d2f4a6c13
Create Date: 2026-05-05 15:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f7a2c9d4e5b6'
down_revision = '9b8d2f4a6c13'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {
        column['name']
        for column in inspector.get_columns('category')
    }

    if 'is_color_auto' not in columns:
        with op.batch_alter_table('category', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'is_color_auto',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false()
            ))

    with op.batch_alter_table('category', schema=None) as batch_op:
        batch_op.alter_column('is_color_auto', server_default=None)


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {
        column['name']
        for column in inspector.get_columns('category')
    }

    if 'is_color_auto' in columns:
        with op.batch_alter_table('category', schema=None) as batch_op:
            batch_op.drop_column('is_color_auto')
