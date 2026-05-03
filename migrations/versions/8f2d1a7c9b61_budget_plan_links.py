"""Add budget plan payment links

Revision ID: 8f2d1a7c9b61
Revises: 4b5203394c54
Create Date: 2026-05-04 02:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '8f2d1a7c9b61'
down_revision = '4b5203394c54'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('expected_expense', schema=None) as batch_op:
        batch_op.add_column(sa.Column('due_day', sa.Integer(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('paid_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('linked_expense_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_expected_expense_linked_expense_id_expense',
            'expense',
            ['linked_expense_id'],
            ['id']
        )

    with op.batch_alter_table('expected_expense', schema=None) as batch_op:
        batch_op.alter_column('due_day', server_default=None)


def downgrade():
    with op.batch_alter_table('expected_expense', schema=None) as batch_op:
        batch_op.drop_constraint('fk_expected_expense_linked_expense_id_expense', type_='foreignkey')
        batch_op.drop_column('linked_expense_id')
        batch_op.drop_column('paid_at')
        batch_op.drop_column('due_day')
