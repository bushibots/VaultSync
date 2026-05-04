"""Add reserved budget buckets

Revision ID: d91a7f6c2b35
Revises: b3c9ea72d114
Create Date: 2026-05-04 20:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd91a7f6c2b35'
down_revision = 'b3c9ea72d114'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    expected_columns = {
        column['name']
        for column in inspector.get_columns('expected_expense')
    }
    expense_columns = {
        column['name']
        for column in inspector.get_columns('expense')
    }
    expense_foreign_keys = inspector.get_foreign_keys('expense')
    has_bucket_fk = any(
        fk.get('referred_table') == 'expected_expense'
        and fk.get('constrained_columns') == ['bucket_id']
        for fk in expense_foreign_keys
    )

    missing_expected_columns = {
        'is_bucket',
        'allocated_amount',
    } - expected_columns

    if missing_expected_columns:
        with op.batch_alter_table('expected_expense', schema=None) as batch_op:
            if 'is_bucket' in missing_expected_columns:
                batch_op.add_column(sa.Column('is_bucket', sa.Boolean(), nullable=False, server_default=sa.false()))
            if 'allocated_amount' in missing_expected_columns:
                batch_op.add_column(sa.Column('allocated_amount', sa.Float(), nullable=False, server_default='0'))

    if 'bucket_id' not in expense_columns:
        with op.batch_alter_table('expense', schema=None) as batch_op:
            batch_op.add_column(sa.Column('bucket_id', sa.Integer(), nullable=True))

    if not has_bucket_fk:
        with op.batch_alter_table('expense', schema=None) as batch_op:
            batch_op.create_foreign_key(
                'fk_expense_bucket_id_expected_expense',
                'expected_expense',
                ['bucket_id'],
                ['id']
            )

    with op.batch_alter_table('expected_expense', schema=None) as batch_op:
        if 'is_bucket' in missing_expected_columns:
            batch_op.alter_column('is_bucket', server_default=None)
        if 'allocated_amount' in missing_expected_columns:
            batch_op.alter_column('allocated_amount', server_default=None)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    expected_columns = {
        column['name']
        for column in inspector.get_columns('expected_expense')
    }
    expense_columns = {
        column['name']
        for column in inspector.get_columns('expense')
    }
    expense_foreign_keys = inspector.get_foreign_keys('expense')
    has_bucket_fk = any(
        fk.get('referred_table') == 'expected_expense'
        and fk.get('constrained_columns') == ['bucket_id']
        for fk in expense_foreign_keys
    )

    with op.batch_alter_table('expense', schema=None) as batch_op:
        if has_bucket_fk:
            batch_op.drop_constraint('fk_expense_bucket_id_expected_expense', type_='foreignkey')
        if 'bucket_id' in expense_columns:
            batch_op.drop_column('bucket_id')

    with op.batch_alter_table('expected_expense', schema=None) as batch_op:
        if 'allocated_amount' in expected_columns:
            batch_op.drop_column('allocated_amount')
        if 'is_bucket' in expected_columns:
            batch_op.drop_column('is_bucket')
