"""Register recyclable models with the generic Recycle Bin engine.

Mirrors io_registrations.py's register_all() -- called once at app startup
(after models + the pilot route modules are imported) from app.py.

Phase 1 (pilot): Employee and PurchaseOrder.
Phase 2 (master data): Buyer, Owner, Item, the five Chart-of-Accounts
levels, Profession, Allowance Type, Purchase/Sales Tax Codes and Unit of
Measurement. Every other module's deletes remain untouched.
"""
from database.routes.recycle_bin import register_recyclable


def register_all_recyclables():
    from models import (Employee, EmployeeAllowance, EmployeeBank,
                        EmployeeDocument, EmployeeProfession,
                        PurchaseOrder, PurchaseOrderLineItem, PurchaseAttachment,
                        BuyerMaster, BuyerBank, BuyerDepartment,
                        Owner, OwnerBank, OwnerDocument, OwnerWarehouse,
                        ItemMaster, ItemUnitMeasurement,
                        LevelOne, LevelTwo, LevelThree, LevelFour, LevelFive,
                        ProfessionMaster, AllowanceType,
                        PurchaseTaxCode, SalesTaxCode, ItemUnit)
    from database.routes.employees import _employee_delete_blockers
    from database.routes.coa import (_level_one_delete_blockers, _level_two_delete_blockers,
                                     _level_three_delete_blockers, _level_four_delete_blockers)
    from database.routes.allowance_types import _allowance_type_delete_blockers
    from database.routes.professions import _profession_delete_blockers

    # ── Employee ──
    register_recyclable(
        'employee', Employee, 'employee', 'employee_master',
        label_fn=lambda e: f'{e.employee_code} - {e.name}',
        children=[
            {'key': 'allowances', 'model': EmployeeAllowance, 'fk': 'employee_id'},
            {'key': 'banks', 'model': EmployeeBank, 'fk': 'employee_id'},
            {'key': 'documents', 'model': EmployeeDocument, 'fk': 'employee_id'},
            {'key': 'professions', 'model': EmployeeProfession, 'fk': 'employee_id'},
        ],
        blocker_fn=_employee_delete_blockers,
        file_path_fields=[('documents', 'file_path')],
    )

    # ── Purchase Order ──
    register_recyclable(
        'purchase_order', PurchaseOrder, 'purchase', 'purchase_order',
        label_fn=lambda po: po.doc_no or f'PO-{po.purchase_order_id}',
        children=[
            {'key': 'line_items', 'model': PurchaseOrderLineItem, 'fk': 'purchase_order_id'},
            {'key': 'attachments', 'model': PurchaseAttachment, 'fk': 'doc_id',
             'extra_filter': {'doc_type': 'PO'}},
        ],
        file_path_fields=[('attachments', 'filepath')],
    )

    # ── Buyer ──
    register_recyclable(
        'buyer', BuyerMaster, 'buyer', 'buyer_master',
        label_fn=lambda b: f'{b.buyer_code} - {b.buyer_name_en}',
        children=[
            {'key': 'banks', 'model': BuyerBank, 'fk': 'buyer_id'},
            {'key': 'departments', 'model': BuyerDepartment, 'fk': 'buyer_id'},
        ],
    )

    # ── Owner ──
    register_recyclable(
        'owner', Owner, 'owner', 'owner_master',
        label_fn=lambda s: f'{s.owner_code} - {s.name}',
        children=[
            {'key': 'banks', 'model': OwnerBank, 'fk': 'owner_id'},
            {'key': 'documents', 'model': OwnerDocument, 'fk': 'owner_id'},
            {'key': 'warehouses', 'model': OwnerWarehouse, 'fk': 'owner_id'},
        ],
        file_path_fields=[('documents', 'file_path')],
    )

    # ── Item ──
    register_recyclable(
        'item', ItemMaster, 'item', 'item_master',
        label_fn=lambda i: f'{i.item_code} - {i.name_en}',
        children=[
            {'key': 'uoms', 'model': ItemUnitMeasurement, 'fk': 'item_id'},
        ],
    )

    # ── Chart of Accounts (Levels 1-5) ──
    register_recyclable(
        'coa_level_one', LevelOne, 'coa', 'level_one',
        label_fn=lambda r: f'{r.code} - {r.drawers}',
        blocker_fn=_level_one_delete_blockers,
    )
    register_recyclable(
        'coa_level_two', LevelTwo, 'coa', 'level_two',
        label_fn=lambda r: f'{r.code} - {r.drawers}',
        blocker_fn=_level_two_delete_blockers,
    )
    register_recyclable(
        'coa_level_three', LevelThree, 'coa', 'level_three',
        label_fn=lambda r: f'{r.code} - {r.drawers}',
        blocker_fn=_level_three_delete_blockers,
    )
    register_recyclable(
        'coa_level_four', LevelFour, 'coa', 'level_four',
        label_fn=lambda r: f'{r.code} - {r.drawers}',
        blocker_fn=_level_four_delete_blockers,
    )
    register_recyclable(
        'coa_level_five', LevelFive, 'coa', 'level_five',
        label_fn=lambda r: f'{r.code} - {r.drawers}',
    )

    # ── Profession ──
    register_recyclable(
        'profession', ProfessionMaster, 'profession', 'profession_master',
        label_fn=lambda p: p.name_en,
        blocker_fn=_profession_delete_blockers,
    )

    # ── Allowance Type ──
    register_recyclable(
        'allowance_type', AllowanceType, 'allowance_type', 'allowance_type_master',
        label_fn=lambda a: f'{a.allowance_code} - {a.allowance_name_en}',
        blocker_fn=_allowance_type_delete_blockers,
    )

    # ── Purchase Tax Code ──
    register_recyclable(
        'purchase_tax_code', PurchaseTaxCode, 'purchase', 'purchase_tax_code',
        label_fn=lambda t: f'{t.tax_code} - {t.section}',
    )

    # ── Sales Tax Code ──
    register_recyclable(
        'sales_tax_code', SalesTaxCode, 'sale', 'sales_tax_code',
        label_fn=lambda t: f'{t.tax_code} - {t.section}',
    )

    # ── Unit of Measurement ──
    register_recyclable(
        'unit_measurement', ItemUnit, 'unit_measurement', 'unit_measurement',
        label_fn=lambda u: f'{u.code} - {u.name_en}',
    )
