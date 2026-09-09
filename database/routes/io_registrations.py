"""
Register master-page models with the generic import/export tool (io_tools).

Import this module's `register_all()` once at app startup (after models are
imported). Purchases and Sales documents are deliberately excluded.

Each entry: register_io(key, model, columns=[(header, attr), ...], unique=attr)
"""

from database.routes.io_tools import register_io, register_bundle


def _to_bool(v):
    return str(v).strip().lower() in ('1', 'true', 'yes', 'y', 'active')


def _to_dec(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


def _to_date(v):
    from datetime import datetime, date, timedelta
    if v in (None, ''):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    # A cell typed/pasted as a plain number (not formatted as a date in
    # Excel) comes back from openpyxl as an int/float Excel serial date
    # (days since 1899-12-30) instead of a datetime -- without this, such
    # a cell silently became None (the field just looked "not imported").
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return (datetime(1899, 12, 30) + timedelta(days=v)).date()
        except (OverflowError, ValueError):
            return None
    s = str(v).strip()
    if not s:
        return None
    # Arabic-Indic digits (١٢٣...) are common in Saudi HR data entered for
    # Iqama-related dates even when the rest of the sheet uses ASCII digits
    # -- without this, e.g. "٢٠٢٦-٠٨-٢٢" fails every strptime pattern below.
    s = s.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%Y/%m/%d', '%d-%m-%Y',
                '%d-%b-%Y', '%d/%b/%Y', '%b %d, %Y', '%b %d %Y', '%d %B %Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def register_all():
    from models import (ItemUnit, AllowanceType, ProfessionMaster,
                        EmployeeWorkAllocation)

    # ── Unit of Measurement ──
    register_io(
        'unit-measurement', ItemUnit,
        columns=[('Code', 'code'), ('Name (EN)', 'name_en'), ('Name (AR)', 'name_ar'),
                 ('Pack Size (EN)', 'pac_size_en'), ('Pack Size (AR)', 'pac_size_ar'),
                 ('Multiply', 'multiply'), ('Status', 'status')],
        unique='code', label='Unit of Measurement',
        coerce={'multiply': _to_dec},
    )

    # ── Allowance Types ──
    register_io(
        'allowance-types', AllowanceType,
        columns=[('Code', 'allowance_code'), ('Name (EN)', 'allowance_name_en'),
                 ('Name (AR)', 'allowance_name_ar'), ('Active', 'is_active')],
        unique='allowance_code', label='Allowance Types',
        coerce={'is_active': _to_bool},
    )

    # ── Professions ──
    register_io(
        'professions', ProfessionMaster,
        columns=[('Name (EN)', 'name_en'), ('Name (AR)', 'name_ar'),
                 ('Active', 'is_active')],
        unique='name_en', label='Professions',
        coerce={'is_active': _to_bool},
    )

    # ── Work Allocations (export-focused; import add-new by employee+month) ──
    register_io(
        'work-allocations', EmployeeWorkAllocation,
        columns=[('Employee ID', 'employee_id'), ('Kafeel', 'kafeel'), ('Name', 'name'),
                 ('Nationality', 'nationality'), ('Profession', 'profession'),
                 ('Iqama', 'iqama'), ('Month', 'month'),
                 ('Joining Date', 'joining_date'), ('End Date', 'end_date'),
                 ('Buyer', 'buyer_name'), ('Department', 'buyer_department'),
                 ('Location', 'location'), ('Shift', 'shift'), ('Status', 'status')],
        unique='iqama', label='Work Allocations',
    )

    # ── Owners ──
    from models import Owner, BuyerMaster, Employee, SupplierMaster, ItemMaster, FinancialYear
    register_io(
        'owners', Owner,
        columns=[('Code', 'owner_code'), ('Name', 'name'), ('Name (AR)', 'name_ar'),
                 ('VAT Number', 'vat_number'), ('CRN', 'crn'), ('Phone', 'phone'),
                 ('Fax', 'fax'), ('Email', 'email'), ('Website', 'website'),
                 ('Building', 'building_number'), ('Street', 'street_name'),
                 ('District', 'district'), ('City', 'city'), ('Postal Code', 'postal_code'),
                 ('Country', 'country'), ('Status', 'status')],
        unique='owner_code', label='Owners',
    )

    # ── Buyers ──
    register_io(
        'buyers', BuyerMaster,
        columns=[('Code', 'buyer_code'), ('Name (EN)', 'buyer_name_en'),
                 ('Name (AR)', 'buyer_name_ar'), ('VAT Number', 'vat_number'),
                 ('CRN', 'crn'), ('Phone', 'phone'), ('Fax', 'fax'), ('Email', 'email'),
                 ('Website', 'website'), ('Building', 'building_number'),
                 ('Street', 'street_name'), ('District', 'district'), ('City', 'city'),
                 ('Postal Code', 'postal_code'), ('Country', 'country'),
                 ('Status', 'status')],
        unique='buyer_code', label='Buyers',
    )

    # ── Suppliers ──
    register_io(
        'suppliers', SupplierMaster,
        columns=[('Code', 'supplier_code'), ('Name (EN)', 'supplier_name_en'),
                 ('Name (AR)', 'supplier_name_ar'), ('VAT Number', 'vat_number'),
                 ('CRN', 'crn'), ('Phone', 'phone'), ('Fax', 'fax'), ('Email', 'email'),
                 ('Website', 'website'), ('Contact Person', 'contact_person'),
                 ('Building', 'building_number'), ('Street', 'street_name'),
                 ('District', 'district'), ('City', 'city'), ('Postal Code', 'postal_code'),
                 ('Country', 'country'), ('Payment Term', 'payment_term'),
                 ('Status', 'status')],
        unique='supplier_code', label='Suppliers',
    )

    # ── Employees ──
    register_io(
        'employees', Employee,
        columns=[('Code', 'employee_code'), ('Name', 'name'), ('Name (AR)', 'name_ar'),
                 ('Nationality', 'nationality'), ('Passport Number', 'passport_number'),
                 ('Iqama Number', 'iqama_number'), ('Mobile', 'mobile'), ('Email', 'email'),
                 ('Joining Date', 'joining_date'), ('Salary Type', 'salary_type'),
                 ('Basic Salary', 'basic_salary'), ('Net Salary', 'net_salary'),
                 ('Active', 'is_active')],
        unique='employee_code', label='Employees',
        coerce={'is_active': _to_bool, 'basic_salary': _to_dec, 'net_salary': _to_dec},
    )

    # ── Item Master ──
    register_io(
        'items', ItemMaster,
        columns=[('Code', 'item_code'), ('Type', 'item_type'), ('Article No', 'article_no'),
                 ('Name (EN)', 'name_en'), ('Name (AR)', 'name_ar'),
                 ('Print Name', 'print_name'), ('UOM', 'uom'), ('Description', 'item_desc'),
                 ('Main Rate', 'main_rate'), ('Retail Rate', 'retail_rate'),
                 ('Wholesale Rate', 'wholesale_rate'), ('MRP', 'mrp'),
                 ('Active', 'is_active'), ('Account Code', 'levelfive_code')],
        unique='item_code', label='Item Master',
        coerce={'is_active': _to_bool, 'main_rate': _to_dec, 'retail_rate': _to_dec,
                'wholesale_rate': _to_dec, 'mrp': _to_dec},
    )

    # ── Financial Year ──
    register_io(
        'financial-year', FinancialYear,
        columns=[('Financial Year', 'financial_year'), ('Range', 'range'),
                 ('Year', 'year'), ('Status', 'status')],
        unique='year', label='Financial Year',
    )

    # ══════════════════════════════════════════════════════════════
    #  Multi-sheet bundles: parent + child tables in one workbook
    # ══════════════════════════════════════════════════════════════
    from models import (OwnerBank, OwnerWarehouse, BuyerBank,
                        SupplierBank, EmployeeBank)

    _bank_cols = [('Bank Name', 'bank_name'), ('Bank Name (AR)', 'bank_name_ar'),
                  ('Account Number', 'account_number'), ('Branch', 'branch'),
                  ('Branch (AR)', 'branch_ar'), ('SWIFT', 'swift_code'),
                  ('IBAN', 'iban'), ('Primary', 'is_primary')]

    # ── Owner + Banks + Warehouses ──
    register_bundle(
        'owner-full', Owner, 'owner_code',
        parent_columns=[('Code', 'owner_code'), ('Name', 'name'), ('Name (AR)', 'name_ar'),
                        ('VAT Number', 'vat_number'), ('CRN', 'crn'), ('Phone', 'phone'),
                        ('Email', 'email'), ('City', 'city'), ('Country', 'country'),
                        ('Status', 'status')],
        children=[
            {'sheet': 'Banks', 'model': OwnerBank, 'fk': 'owner_id',
             'parent_ref': 'Owner Code', 'columns': _bank_cols,
             'coerce': {'is_primary': _to_bool}},
            {'sheet': 'Warehouses', 'model': OwnerWarehouse, 'fk': 'owner_id',
             'parent_ref': 'Owner Code',
             'columns': [('Warehouse Name', 'warehouse_name'),
                         ('Warehouse Name (AR)', 'warehouse_name_ar'),
                         ('Location', 'location'), ('Location (AR)', 'location_ar')]},
        ],
        label='Owners',
    )

    # ── Buyer + Banks ──
    register_bundle(
        'buyer-full', BuyerMaster, 'buyer_code',
        parent_columns=[('Code', 'buyer_code'), ('Name (EN)', 'buyer_name_en'),
                        ('Name (AR)', 'buyer_name_ar'), ('VAT Number', 'vat_number'),
                        ('CRN', 'crn'), ('Phone', 'phone'), ('Email', 'email'),
                        ('City', 'city'), ('Country', 'country'), ('Status', 'status')],
        children=[
            {'sheet': 'Banks', 'model': BuyerBank, 'fk': 'buyer_id',
             'parent_ref': 'Buyer Code', 'columns': _bank_cols,
             'coerce': {'is_primary': _to_bool}},
        ],
        label='Buyers',
    )

    # ── Supplier + Banks (supplier bank uses *_en field names) ──
    register_bundle(
        'supplier-full', SupplierMaster, 'supplier_code',
        parent_columns=[('Code', 'supplier_code'), ('Name (EN)', 'supplier_name_en'),
                        ('Name (AR)', 'supplier_name_ar'), ('VAT Number', 'vat_number'),
                        ('CRN', 'crn'), ('Phone', 'phone'), ('Email', 'email'),
                        ('City', 'city'), ('Country', 'country'), ('Status', 'status')],
        children=[
            {'sheet': 'Banks', 'model': SupplierBank, 'fk': 'supplier_id',
             'parent_ref': 'Supplier Code',
             'columns': [('Bank Name', 'bank_name_en'), ('Bank Name (AR)', 'bank_name_ar'),
                         ('Account Number', 'account_number'), ('Branch', 'branch_en'),
                         ('Branch (AR)', 'branch_ar'), ('SWIFT', 'swift_code'),
                         ('IBAN', 'iban'), ('Primary', 'is_primary')],
             'coerce': {'is_primary': _to_bool}},
        ],
        label='Suppliers',
    )

    # ── Employee + Banks + Professions + Allowances + Allowance Types ──
    from models import (EmployeeAllowance, EmployeeProfession, AllowanceType,
                        ProfessionMaster)

    def _resolve_profession(obj, row, pid):
        """Find or create the ProfessionMaster and set profession_id."""
        from models import db, ProfessionMaster
        nm = (obj.profession_name or '').strip()
        if not nm:
            return False
        pm = ProfessionMaster.query.filter_by(name_en=nm).first()
        if not pm:
            pm = ProfessionMaster(name_en=nm,
                                  name_ar=(obj.profession_name_ar or None),
                                  is_active=True)
            db.session.add(pm); db.session.flush()
        obj.profession_id = pm.id
        return True

    def _resolve_allowance(obj, row, pid):
        """Find or create the AllowanceType by code and set allowance_type_id."""
        from models import db, AllowanceType
        code = (obj.allowance_code or '').strip()
        if not code:
            return True   # amount-only allowance is allowed
        at = AllowanceType.query.filter_by(allowance_code=code).first()
        if not at:
            at = AllowanceType(allowance_code=code,
                               allowance_name_en=(obj.name or code),
                               allowance_name_ar=(obj.name_ar or None),
                               is_active=True)
            db.session.add(at); db.session.flush()
        obj.allowance_type_id = at.id
        return True

    register_bundle(
        'employee-full', Employee, 'employee_code',
        parent_columns=[
            ('Code', 'employee_code'), ('Auto Code', 'auto_code'),
            ('Name', 'name'), ('Name (AR)', 'name_ar'),
            ('Active', 'is_active'), ('Muslim', 'is_muslim'), ('Blood Group', 'blood_group'),
            ('Nationality', 'nationality'), ('Nationality (AR)', 'nationality_ar'),
            ('Education', 'education'), ('Education (AR)', 'education_ar'),
            ('Kafeel', 'kafeel_name'), ('Kafeel (AR)', 'kafeel_name_ar'),
            ('Kafeel Reference', 'kafeel_reference'), ('Kafeel Reference (AR)', 'kafeel_reference_ar'),
            ('Kafalat Number', 'kafalat_number'),
            ('Passport Number', 'passport_number'), ('Passport Expiry', 'passport_expiry'),
            ('Passport Location', 'passport_location'),
            ('Entry Number', 'entry_number'),
            ('Iqama Number', 'iqama_number'), ('Iqama Expiry', 'iqama_expiry'),
            ('Arrival Date', 'arrival_date'), ('Birth Date', 'birth_date'),
            ('Mobile', 'mobile'), ('Email', 'email'),
            ('Address', 'address'), ('Address (AR)', 'address_ar'),
            ('Home City', 'home_city'), ('Home City (AR)', 'home_city_ar'),
            ('Employee Reference', 'employee_reference'), ('Employee Reference (AR)', 'employee_reference_ar'),
            ('Salary Category', 'salary_category'), ('Salary Type', 'salary_type'),
            ('Basic Salary', 'basic_salary'), ('Total Allowances', 'total_allowances'),
            ('Net Salary', 'net_salary'),
            ('PO Number', 'po_number'), ('Services Charges', 'services_charges'),
            ('PO Rate', 'po_rate'), ('PO OT Rate', 'po_ot_rate'),
            ('Working Hours', 'working_hours'),
            ('Overtime Ratio', 'overtime_ratio'), ('Overtime Rate', 'overtime_rate'),
            ('Hostel Name', 'hostel_name'), ('Hostel Name (AR)', 'hostel_name_ar'),
            ('Room Number', 'room_number'),
            ('Hostel Location', 'hostel_location'), ('Hostel Location (AR)', 'hostel_location_ar'),
            ('CRN', 'crn'), ('CRN (AR)', 'crn_ar'),
            ('Insurance Company', 'insurance_company'), ('Insurance Company (AR)', 'insurance_company_ar'),
            ('Insurance Expiry', 'insurance_expiry'),
            ('Labour Office', 'labour_office'),
        ],
        parent_coerce={
            'is_active': _to_bool, 'is_muslim': _to_bool, 'auto_code': _to_bool,
            'passport_expiry': _to_date, 'iqama_expiry': _to_date, 'arrival_date': _to_date,
            'birth_date': _to_date, 'insurance_expiry': _to_date,
            'basic_salary': _to_dec, 'total_allowances': _to_dec, 'net_salary': _to_dec,
            'po_rate': _to_dec, 'po_ot_rate': _to_dec, 'services_charges': _to_dec,
            'working_hours': _to_dec, 'overtime_ratio': _to_dec,
            'overtime_rate': _to_dec,
        },
        children=[
            {'sheet': 'Banks', 'model': EmployeeBank, 'fk': 'employee_id',
             'parent_ref': 'Employee Code', 'columns': _bank_cols,
             'coerce': {'is_primary': _to_bool}},
            {'sheet': 'Professions', 'model': EmployeeProfession, 'fk': 'employee_id',
             'parent_ref': 'Employee Code',
             'columns': [('Profession (EN)', 'profession_name'),
                         ('Profession (AR)', 'profession_name_ar')],
             'resolve': _resolve_profession, 'dedupe': ['profession_id']},
            {'sheet': 'Allowances', 'model': EmployeeAllowance, 'fk': 'employee_id',
             'parent_ref': 'Employee Code',
             'columns': [('Allowance Code', 'allowance_code'), ('Name', 'name'),
                         ('Name (AR)', 'name_ar'), ('Amount', 'amount')],
             'coerce': {'amount': _to_dec},
             'resolve': _resolve_allowance, 'dedupe': ['allowance_type_id']},
            {'sheet': 'Allowance Types', 'model': AllowanceType, 'standalone': True,
             'unique': 'allowance_code',
             'columns': [('Allowance Code', 'allowance_code'),
                         ('Allowance Name (EN)', 'allowance_name_en'),
                         ('Allowance Name (AR)', 'allowance_name_ar'),
                         ('Active', 'is_active')],
             'coerce': {'is_active': _to_bool}},
        ],
        label='Employees',
    )

    # ══════════════════════════════════════════════════════════════
    #  Child tables as their OWN standalone import/export files.
    #  Each carries a parent-code column so rows link back on import.
    #  unique=None -> no dedupe (children have no natural unique key);
    #  every row imports and attaches to its parent by code.
    # ══════════════════════════════════════════════════════════════

    # Owner Banks
    register_io(
        'owner-banks', OwnerBank,
        columns=[('Owner Code', '_parent'), ('Bank Name', 'bank_name'),
                 ('Bank Name (AR)', 'bank_name_ar'), ('Account Number', 'account_number'),
                 ('Branch', 'branch'), ('Branch (AR)', 'branch_ar'),
                 ('SWIFT', 'swift_code'), ('IBAN', 'iban'), ('Primary', 'is_primary')],
        unique=None, label='Owner Banks',
        parents=[('Owner Code', '_parent', Owner, 'owner_code', 'owner_id')],
        coerce={'is_primary': _to_bool},
    )

    # Owner Warehouses
    register_io(
        'owner-warehouses', OwnerWarehouse,
        columns=[('Owner Code', '_parent'), ('Warehouse Name', 'warehouse_name'),
                 ('Warehouse Name (AR)', 'warehouse_name_ar'),
                 ('Location', 'location'), ('Location (AR)', 'location_ar')],
        unique=None, label='Owner Warehouses',
        parents=[('Owner Code', '_parent', Owner, 'owner_code', 'owner_id')],
    )

    # Buyer Banks
    register_io(
        'buyer-banks', BuyerBank,
        columns=[('Buyer Code', '_parent'), ('Bank Name', 'bank_name'),
                 ('Bank Name (AR)', 'bank_name_ar'), ('Account Number', 'account_number'),
                 ('Branch', 'branch'), ('Branch (AR)', 'branch_ar'),
                 ('SWIFT', 'swift_code'), ('IBAN', 'iban'), ('Primary', 'is_primary')],
        unique=None, label='Buyer Banks',
        parents=[('Buyer Code', '_parent', BuyerMaster, 'buyer_code', 'buyer_id')],
        coerce={'is_primary': _to_bool},
    )

    # Supplier Banks (uses *_en field names)
    register_io(
        'supplier-banks', SupplierBank,
        columns=[('Supplier Code', '_parent'), ('Bank Name', 'bank_name_en'),
                 ('Bank Name (AR)', 'bank_name_ar'), ('Account Number', 'account_number'),
                 ('Branch', 'branch_en'), ('Branch (AR)', 'branch_ar'),
                 ('SWIFT', 'swift_code'), ('IBAN', 'iban'), ('Primary', 'is_primary')],
        unique=None, label='Supplier Banks',
        parents=[('Supplier Code', '_parent', SupplierMaster, 'supplier_code', 'supplier_id')],
        coerce={'is_primary': _to_bool},
    )

    # Employee Banks
    register_io(
        'employee-banks', EmployeeBank,
        columns=[('Employee Code', '_parent'), ('Bank Name', 'bank_name'),
                 ('Bank Name (AR)', 'bank_name_ar'), ('Account Number', 'account_number'),
                 ('Branch', 'branch'), ('Branch (AR)', 'branch_ar'),
                 ('SWIFT', 'swift_code'), ('IBAN', 'iban'), ('Primary', 'is_primary')],
        unique=None, label='Employee Banks',
        parents=[('Employee Code', '_parent', Employee, 'employee_code', 'employee_id')],
        coerce={'is_primary': _to_bool},
    )

    # ══════════════════════════════════════════════════════════════
    #  Payroll (Salary Consolidation) and Journal Entry
    # ══════════════════════════════════════════════════════════════
    from models import SalaryConsolidation, JournalEntry

    # Salary Consolidation — export + import (add-new by sheet_no)
    register_io(
        'salary-consolidation', SalaryConsolidation,
        columns=[('Sheet No', 'sheet_no'), ('Month', 'month'), ('Employee Name', 'employee_name'),
                 ('Profession', 'profession'), ('Nationality', 'nationality'),
                 ('Iqama', 'iqama'), ('Buyer Department', 'buyer_department'), ('Kafeel', 'kafeel'),
                 ('Day Hour', 'day_hour'), ('Allowance', 'allowance'),
                 ('Days', 'days'), ('Fridays', 'fridays'), ('Holidays', 'holidays'),
                 ('Absent', 'absent'), ('Monthly Salary', 'monthly_salary'),
                 ('OT Hours', 'ot_hour'), ('OT Amount', 'ot_amount'), ('Bonus', 'bonus'),
                 ('Total Salary', 'total_salary'), ('Advance', 'advance'),
                 ('Salary Payable', 'salary_payable'), ('Status', 'status'),
                 ('Bank Code', 'bank_code'), ('IBAN', 'iban_no')],
        unique='sheet_no', label='Salary Consolidation',
        coerce={'day_hour': _to_dec, 'allowance': _to_dec, 'days': _to_dec,
                'monthly_salary': _to_dec, 'ot_hour': _to_dec, 'ot_amount': _to_dec,
                'bonus': _to_dec, 'total_salary': _to_dec, 'advance': _to_dec,
                'salary_payable': _to_dec},
    )

    # Journal Entry — EXPORT ONLY in practice (page is view-only).
    register_io(
        'journal-entries', JournalEntry,
        columns=[('JE No', 'je_no'), ('Origin', 'origion'), ('Origin Type', 'origin_type'),
                 ('Reference', 'refrence'), ('Posting Date', 'posting_date'),
                 ('Due Date', 'due_date'), ('Document Date', 'document_date'),
                 ('Narration', 'narration')],
        unique='je_no', label='Journal Entries',
    )