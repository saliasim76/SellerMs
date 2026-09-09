"""convert Arabic (_ar) String/Text columns to Unicode/NVARCHAR

Revision ID: f8c2b6d3a917
Revises: e7b3f5a91d24
Create Date: 2026-08-12 00:00:00.000000

Fixes: Arabic text not saving correctly. Every *_ar column was declared
with db.String/db.Text, which SQLAlchemy compiles to VARCHAR/TEXT on the
mssql dialect -- not Unicode-safe. Converting to NVARCHAR (via
db.Unicode/db.UnicodeText in models.py, and ALTER COLUMN here) fixes it
for all future writes.

IMPORTANT: this does NOT recover Arabic data that was already corrupted
by earlier VARCHAR writes (silently replaced with '?' or lost depending
on your server's collation). Check existing _ar columns for rows full of
'?' before/after this migration -- those need to be re-entered by hand,
there is no way to recover the original characters from a VARCHAR column
that already lost them.
"""
from alembic import op
import sqlalchemy as sa


revision = 'f8c2b6d3a917'
down_revision = 'e7b3f5a91d24'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('buyer_banks', schema=None) as batch_op:
        batch_op.alter_column('bank_name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))
        batch_op.alter_column('branch_ar', type_=sa.Unicode(100), existing_type=sa.String(100))

    with op.batch_alter_table('buyer_departments', schema=None) as batch_op:
        batch_op.alter_column('department_name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))
        batch_op.alter_column('location_name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))

    with op.batch_alter_table('buyers', schema=None) as batch_op:
        batch_op.alter_column('buyer_name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('street_name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('country_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('city_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('district_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('levelfive_drawer_ar', type_=sa.Unicode(250), existing_type=sa.String(250))

    with op.batch_alter_table('employee_allowance_types', schema=None) as batch_op:
        batch_op.alter_column('allowance_name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))

    with op.batch_alter_table('employee_allowances', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))

    with op.batch_alter_table('employee_banks', schema=None) as batch_op:
        batch_op.alter_column('bank_name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))
        batch_op.alter_column('branch_ar', type_=sa.Unicode(120), existing_type=sa.String(120))

    with op.batch_alter_table('employee_professions', schema=None) as batch_op:
        batch_op.alter_column('profession_name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))

    with op.batch_alter_table('employee_work_allocation', schema=None) as batch_op:
        batch_op.alter_column('buyer_name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('buyer_department_ar', type_=sa.Unicode(150), existing_type=sa.String(150))
        batch_op.alter_column('location_ar', type_=sa.Unicode(150), existing_type=sa.String(150))

    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('nationality_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('education_ar', type_=sa.Unicode(150), existing_type=sa.String(150))
        batch_op.alter_column('kafeel_name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('kafeel_reference_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('address_ar', type_=sa.Unicode(300), existing_type=sa.String(300))
        batch_op.alter_column('home_city_ar', type_=sa.Unicode(120), existing_type=sa.String(120))
        batch_op.alter_column('employee_reference_ar', type_=sa.Unicode(120), existing_type=sa.String(120))
        batch_op.alter_column('hostel_name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('hostel_location_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('crn_ar', type_=sa.Unicode(60), existing_type=sa.String(60))
        batch_op.alter_column('insurance_company_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('levelfive_drawer_ar', type_=sa.Unicode(250), existing_type=sa.String(250))

    with op.batch_alter_table('grl_detail', schema=None) as batch_op:
        batch_op.alter_column('account_name_ar', type_=sa.Unicode(250), existing_type=sa.String(250))

    with op.batch_alter_table('item_categories', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.Unicode(100), existing_type=sa.String(100))

    with op.batch_alter_table('item_master', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('levelfive_drawer_ar', type_=sa.Unicode(250), existing_type=sa.String(250))
        batch_op.alter_column('levelfive_drawer_ar1', type_=sa.Unicode(250), existing_type=sa.String(250))
        batch_op.alter_column('item_name_ar', type_=sa.Unicode(250), existing_type=sa.String(250))

    with op.batch_alter_table('item_sub_categories', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.Unicode(100), existing_type=sa.String(100))

    with op.batch_alter_table('level_five', schema=None) as batch_op:
        batch_op.alter_column('drawers_ar', type_=sa.Unicode(250), existing_type=sa.String(250))
        batch_op.alter_column('description_ar', type_=sa.Unicode(255), existing_type=sa.String(255))

    with op.batch_alter_table('level_four', schema=None) as batch_op:
        batch_op.alter_column('drawers_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('description_ar', type_=sa.Unicode(255), existing_type=sa.String(255))

    with op.batch_alter_table('level_one', schema=None) as batch_op:
        batch_op.alter_column('drawers_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('description_ar', type_=sa.Unicode(255), existing_type=sa.String(255))

    with op.batch_alter_table('level_three', schema=None) as batch_op:
        batch_op.alter_column('drawers_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('description_ar', type_=sa.Unicode(255), existing_type=sa.String(255))

    with op.batch_alter_table('level_two', schema=None) as batch_op:
        batch_op.alter_column('drawers_ar', type_=sa.Unicode(150), existing_type=sa.String(150))
        batch_op.alter_column('description_ar', type_=sa.Unicode(255), existing_type=sa.String(255))

    with op.batch_alter_table('profession_master', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))

    with op.batch_alter_table('seller_banks', schema=None) as batch_op:
        batch_op.alter_column('bank_name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))
        batch_op.alter_column('branch_ar', type_=sa.Unicode(100), existing_type=sa.String(100))

    with op.batch_alter_table('seller_warehouses', schema=None) as batch_op:
        batch_op.alter_column('warehouse_name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('location_ar', type_=sa.Unicode(200), existing_type=sa.String(200))

    with op.batch_alter_table('sellers', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('street_name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('district_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('city_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('country_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('levelfive_drawer_ar', type_=sa.Unicode(250), existing_type=sa.String(250))

    with op.batch_alter_table('tax_categories', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.Unicode(100), existing_type=sa.String(100))

    with op.batch_alter_table('unit_measurement', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))
        batch_op.alter_column('pac_size_ar', type_=sa.Unicode(150), existing_type=sa.String(150))

    with op.batch_alter_table('unit_of_measurement', schema=None) as batch_op:
        batch_op.alter_column('unit_name_ar', type_=sa.Unicode(50), existing_type=sa.String(50))

    with op.batch_alter_table('vendor_banks', schema=None) as batch_op:
        batch_op.alter_column('bank_name_ar', type_=sa.Unicode(150), existing_type=sa.String(150))
        batch_op.alter_column('branch_ar', type_=sa.Unicode(100), existing_type=sa.String(100))

    with op.batch_alter_table('vendors', schema=None) as batch_op:
        batch_op.alter_column('vendor_name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('street_name_ar', type_=sa.Unicode(200), existing_type=sa.String(200))
        batch_op.alter_column('country_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('city_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('district_ar', type_=sa.Unicode(100), existing_type=sa.String(100))
        batch_op.alter_column('levelfive_drawer_ar', type_=sa.Unicode(250), existing_type=sa.String(250))


def downgrade():
    with op.batch_alter_table('buyer_banks', schema=None) as batch_op:
        batch_op.alter_column('bank_name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))
        batch_op.alter_column('branch_ar', type_=sa.String(100), existing_type=sa.Unicode(100))

    with op.batch_alter_table('buyer_departments', schema=None) as batch_op:
        batch_op.alter_column('department_name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))
        batch_op.alter_column('location_name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))

    with op.batch_alter_table('buyers', schema=None) as batch_op:
        batch_op.alter_column('buyer_name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('street_name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('country_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('city_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('district_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('levelfive_drawer_ar', type_=sa.String(250), existing_type=sa.Unicode(250))

    with op.batch_alter_table('employee_allowance_types', schema=None) as batch_op:
        batch_op.alter_column('allowance_name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))

    with op.batch_alter_table('employee_allowances', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))

    with op.batch_alter_table('employee_banks', schema=None) as batch_op:
        batch_op.alter_column('bank_name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))
        batch_op.alter_column('branch_ar', type_=sa.String(120), existing_type=sa.Unicode(120))

    with op.batch_alter_table('employee_professions', schema=None) as batch_op:
        batch_op.alter_column('profession_name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))

    with op.batch_alter_table('employee_work_allocation', schema=None) as batch_op:
        batch_op.alter_column('buyer_name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('buyer_department_ar', type_=sa.String(150), existing_type=sa.Unicode(150))
        batch_op.alter_column('location_ar', type_=sa.String(150), existing_type=sa.Unicode(150))

    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('nationality_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('education_ar', type_=sa.String(150), existing_type=sa.Unicode(150))
        batch_op.alter_column('kafeel_name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('kafeel_reference_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('address_ar', type_=sa.String(300), existing_type=sa.Unicode(300))
        batch_op.alter_column('home_city_ar', type_=sa.String(120), existing_type=sa.Unicode(120))
        batch_op.alter_column('employee_reference_ar', type_=sa.String(120), existing_type=sa.Unicode(120))
        batch_op.alter_column('hostel_name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('hostel_location_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('crn_ar', type_=sa.String(60), existing_type=sa.Unicode(60))
        batch_op.alter_column('insurance_company_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('levelfive_drawer_ar', type_=sa.String(250), existing_type=sa.Unicode(250))

    with op.batch_alter_table('grl_detail', schema=None) as batch_op:
        batch_op.alter_column('account_name_ar', type_=sa.String(250), existing_type=sa.Unicode(250))

    with op.batch_alter_table('item_categories', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.String(100), existing_type=sa.Unicode(100))

    with op.batch_alter_table('item_master', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('levelfive_drawer_ar', type_=sa.String(250), existing_type=sa.Unicode(250))
        batch_op.alter_column('levelfive_drawer_ar1', type_=sa.String(250), existing_type=sa.Unicode(250))
        batch_op.alter_column('item_name_ar', type_=sa.String(250), existing_type=sa.Unicode(250))

    with op.batch_alter_table('item_sub_categories', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.String(100), existing_type=sa.Unicode(100))

    with op.batch_alter_table('level_five', schema=None) as batch_op:
        batch_op.alter_column('drawers_ar', type_=sa.String(250), existing_type=sa.Unicode(250))
        batch_op.alter_column('description_ar', type_=sa.String(255), existing_type=sa.Unicode(255))

    with op.batch_alter_table('level_four', schema=None) as batch_op:
        batch_op.alter_column('drawers_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('description_ar', type_=sa.String(255), existing_type=sa.Unicode(255))

    with op.batch_alter_table('level_one', schema=None) as batch_op:
        batch_op.alter_column('drawers_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('description_ar', type_=sa.String(255), existing_type=sa.Unicode(255))

    with op.batch_alter_table('level_three', schema=None) as batch_op:
        batch_op.alter_column('drawers_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('description_ar', type_=sa.String(255), existing_type=sa.Unicode(255))

    with op.batch_alter_table('level_two', schema=None) as batch_op:
        batch_op.alter_column('drawers_ar', type_=sa.String(150), existing_type=sa.Unicode(150))
        batch_op.alter_column('description_ar', type_=sa.String(255), existing_type=sa.Unicode(255))

    with op.batch_alter_table('profession_master', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))

    with op.batch_alter_table('seller_banks', schema=None) as batch_op:
        batch_op.alter_column('bank_name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))
        batch_op.alter_column('branch_ar', type_=sa.String(100), existing_type=sa.Unicode(100))

    with op.batch_alter_table('seller_warehouses', schema=None) as batch_op:
        batch_op.alter_column('warehouse_name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('location_ar', type_=sa.String(200), existing_type=sa.Unicode(200))

    with op.batch_alter_table('sellers', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('street_name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('district_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('city_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('country_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('levelfive_drawer_ar', type_=sa.String(250), existing_type=sa.Unicode(250))

    with op.batch_alter_table('tax_categories', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.String(100), existing_type=sa.Unicode(100))

    with op.batch_alter_table('unit_measurement', schema=None) as batch_op:
        batch_op.alter_column('name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))
        batch_op.alter_column('pac_size_ar', type_=sa.String(150), existing_type=sa.Unicode(150))

    with op.batch_alter_table('unit_of_measurement', schema=None) as batch_op:
        batch_op.alter_column('unit_name_ar', type_=sa.String(50), existing_type=sa.Unicode(50))

    with op.batch_alter_table('vendor_banks', schema=None) as batch_op:
        batch_op.alter_column('bank_name_ar', type_=sa.String(150), existing_type=sa.Unicode(150))
        batch_op.alter_column('branch_ar', type_=sa.String(100), existing_type=sa.Unicode(100))

    with op.batch_alter_table('vendors', schema=None) as batch_op:
        batch_op.alter_column('vendor_name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('street_name_ar', type_=sa.String(200), existing_type=sa.Unicode(200))
        batch_op.alter_column('country_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('city_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('district_ar', type_=sa.String(100), existing_type=sa.Unicode(100))
        batch_op.alter_column('levelfive_drawer_ar', type_=sa.String(250), existing_type=sa.Unicode(250))

