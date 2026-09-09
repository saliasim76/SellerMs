"""rebuild salary_consolidation to match the payroll spec exactly

Revision ID: 9f3c2a7e5d10
Revises: 94a40140de6c
Create Date: 2026-08-08 00:00:00.000000

Drops the old salary_consolidation table entirely (data loss, confirmed
with the user) and recreates it with the field names/shape used by the
rebuilt payroll module (models.SalaryConsolidation) — including
employee_code and payment_status, which were originally added in two
follow-up migrations (b2e8f114a9c3, c7a1d2f9e5b8) now folded into this
single migration.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9f3c2a7e5d10'
down_revision = '94a40140de6c'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('salary_consolidation')

    op.create_table(
        'salary_consolidation',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('payroll_id', sa.String(length=20), index=True),
        sa.Column('payroll_status', sa.String(length=10)),
        sa.Column('salary_order', sa.String(length=30)),
        sa.Column('month_from', sa.Date()),
        sa.Column('month_to', sa.Date()),
        sa.Column('month', sa.String(length=20)),
        sa.Column('kafeel', sa.String(length=200)),
        sa.Column('buyer_id', sa.Integer()),
        sa.Column('buyer_name', sa.String(length=200)),
        sa.Column('buyer_department', sa.String(length=150)),
        sa.Column('location', sa.String(length=150)),
        sa.Column('sheet_no', sa.String(length=30)),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id')),
        sa.Column('employee_code', sa.String(length=20), nullable=True),
        sa.Column('employee_name', sa.String(length=200)),
        sa.Column('profession', sa.String(length=150)),
        sa.Column('nationality', sa.String(length=100)),
        sa.Column('iqama', sa.String(length=50)),
        sa.Column('salary_category', sa.String(length=30)),
        sa.Column('salary_type', sa.String(length=20)),
        sa.Column('day_hour', sa.Numeric(10, 2)),
        sa.Column('basic_salary', sa.Numeric(12, 2)),
        sa.Column('allowance', sa.Numeric(12, 2)),
        sa.Column('days', sa.Integer()),
        sa.Column('fridays', sa.Integer()),
        sa.Column('holidays', sa.Integer()),
        sa.Column('absent', sa.Integer()),
        sa.Column('monthly_salary', sa.Numeric(12, 2)),
        sa.Column('total_hours', sa.Numeric(10, 2)),
        sa.Column('working_hour', sa.Numeric(10, 2)),
        sa.Column('ot_hour', sa.Numeric(10, 2)),
        sa.Column('extra_ot', sa.Numeric(10, 2)),
        sa.Column('ot_rate', sa.Numeric(12, 2)),
        sa.Column('ot_amount', sa.Numeric(12, 2)),
        sa.Column('bonus', sa.Numeric(12, 2)),
        sa.Column('deduction', sa.Numeric(12, 2)),
        sa.Column('total_salary', sa.Numeric(12, 2)),
        sa.Column('advance', sa.Numeric(12, 2)),
        sa.Column('credit', sa.Numeric(12, 2)),
        sa.Column('salary_payable', sa.Numeric(12, 2)),
        sa.Column('paid', sa.Numeric(12, 2)),
        sa.Column('balance', sa.Numeric(12, 2)),
        sa.Column('employ_payroll_status', sa.String(length=10)),
        sa.Column('payment_status', sa.String(length=10), nullable=True),
        sa.Column('iqama_expiry', sa.Date()),
        sa.Column('status', sa.String(length=20)),
        sa.Column('bank_code', sa.String(length=60)),
        sa.Column('iban_no', sa.String(length=60)),
        sa.Column('po_rate', sa.Numeric(12, 2)),
        sa.Column('invoice_amount', sa.Numeric(12, 2)),
        sa.Column('created_at', sa.DateTime()),
        sa.Column('created_by', sa.Integer()),
    )


def downgrade():
    op.drop_table('salary_consolidation')
    # Note: this downgrade recreates only an empty table with the OLD shape's
    # primary/foreign keys; the original data cannot be restored since
    # upgrade() dropped it. Restore from a backup if you need the old data.
    op.create_table(
        'salary_consolidation',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id')),
    )
