"""add warehouses, departments, locations, buyer salary_order

Revision ID: add_wh_dept_001
Revises: <PUT_YOUR_CURRENT_HEAD_HERE>   # run: flask db heads
"""
from alembic import op
import sqlalchemy as sa

revision = 'add_wh_dept_001'
down_revision = '<PUT_YOUR_CURRENT_HEAD_HERE>'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('warehouse_locations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('location_name', sa.String(150), nullable=False),
        sa.Column('location_name_ar', sa.String(150)),
        sa.Column('is_active', sa.Boolean(), server_default=sa.true()),
        sa.Column('created_at', sa.DateTime()),
    )
    op.create_table('warehouses',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('seller_id', sa.Integer(),
                  sa.ForeignKey('sellers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('warehouse_name', sa.String(200), nullable=False),
        sa.Column('warehouse_name_ar', sa.String(200)),
        sa.Column('location_id', sa.Integer(), sa.ForeignKey('warehouse_locations.id')),
        sa.Column('created_at', sa.DateTime()),
    )
    op.create_table('department_locations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('location_name', sa.String(150), nullable=False),
        sa.Column('location_name_ar', sa.String(150)),
        sa.Column('is_active', sa.Boolean(), server_default=sa.true()),
        sa.Column('created_at', sa.DateTime()),
    )
    op.create_table('departments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('buyer_id', sa.Integer(),
                  sa.ForeignKey('buyers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('department_name', sa.String(200), nullable=False),
        sa.Column('department_name_ar', sa.String(200)),
        sa.Column('location_id', sa.Integer(), sa.ForeignKey('department_locations.id')),
        sa.Column('created_at', sa.DateTime()),
    )
    op.add_column('buyers', sa.Column('salary_order', sa.Integer(), server_default='1'))


def downgrade():
    op.drop_column('buyers', 'salary_order')
    op.drop_table('departments')
    op.drop_table('department_locations')
    op.drop_table('warehouses')
    op.drop_table('warehouse_locations')