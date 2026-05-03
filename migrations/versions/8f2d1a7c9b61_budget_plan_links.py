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
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('expected_expense')}
    foreign_keys = inspector.get_foreign_keys('expected_expense')
    has_linked_expense_fk = any(
        fk.get('referred_table') == 'expense'
        and fk.get('constrained_columns') == ['linked_expense_id']
        for fk in foreign_keys
    )

    missing_columns = {
        'due_day',
        'paid_at',
        'linked_expense_id',
    } - columns

    if missing_columns:
        with op.batch_alter_table('expected_expense', schema=None) as batch_op:
            if 'due_day' in missing_columns:
                batch_op.add_column(sa.Column('due_day', sa.Integer(), nullable=False, server_default='1'))
            if 'paid_at' in missing_columns:
                batch_op.add_column(sa.Column('paid_at', sa.DateTime(), nullable=True))
            if 'linked_expense_id' in missing_columns:
                batch_op.add_column(sa.Column('linked_expense_id', sa.Integer(), nullable=True))

    if not has_linked_expense_fk:
        with op.batch_alter_table('expected_expense', schema=None) as batch_op:
            batch_op.create_foreign_key(
                'fk_expected_expense_linked_expense_id_expense',
                'expense',
                ['linked_expense_id'],
                ['id']
            )

    if 'due_day' in missing_columns:
        with op.batch_alter_table('expected_expense', schema=None) as batch_op:
            batch_op.alter_column('due_day', server_default=None)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('expected_expense')}
    foreign_keys = inspector.get_foreign_keys('expected_expense')
    has_linked_expense_fk = any(
        fk.get('referred_table') == 'expense'
        and fk.get('constrained_columns') == ['linked_expense_id']
        for fk in foreign_keys
    )

    with op.batch_alter_table('expected_expense', schema=None) as batch_op:
        if has_linked_expense_fk:
            batch_op.drop_constraint('fk_expected_expense_linked_expense_id_expense', type_='foreignkey')
        if 'linked_expense_id' in columns:
            batch_op.drop_column('linked_expense_id')
        if 'paid_at' in columns:
            batch_op.drop_column('paid_at')
        if 'due_day' in columns:
            batch_op.drop_column('due_day')
