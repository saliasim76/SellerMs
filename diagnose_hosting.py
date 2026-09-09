"""
Standalone hosting diagnostic -- run this directly on the cPanel host to find
out why Import/Export routes are 500ing there.

How to run on cPanel:
  1. Upload this file next to passenger_wsgi.py (same directory as app.py).
  2. cPanel -> Setup Python App -> your app -> copy the "Enter to the virtual
     environment" command it shows you (something like:
       source /home/USER/virtualenv/APPDIR/3.x/bin/activate && cd /home/USER/APPDIR
     ) and run it in cPanel's Terminal (or SSH if you have it).
  3. Run:  python diagnose_hosting.py
  4. Paste the full output back.

This does NOT touch your database or your live app -- it only inspects the
installed Python packages and does a couple of harmless in-memory checks
(building a tiny .xlsx in memory, checking send_file()'s signature). Nothing
is written to disk.
"""
import sys
import os

print('=' * 70)
print('PYTHON')
print('=' * 70)
print('sys.executable:', sys.executable)
print('sys.version:', sys.version)
print()

print('=' * 70)
print('INSTALLED PACKAGE VERSIONS vs requirements.txt')
print('=' * 70)

try:
    from importlib import metadata as importlib_metadata
except ImportError:
    import importlib_metadata  # type: ignore

req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'requirements.txt')
wanted = []
if os.path.exists(req_path):
    with open(req_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '==' in line:
                name, version = line.split('==', 1)
                wanted.append((name.strip(), version.strip()))
else:
    print(f'  (requirements.txt not found next to this script at {req_path})')

mismatches = []
missing = []
for name, wanted_version in wanted:
    try:
        installed_version = importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        missing.append(name)
        print(f'  {name:<20} MISSING (requirements.txt wants {wanted_version})')
        continue
    marker = '' if installed_version == wanted_version else '  <-- MISMATCH'
    if marker:
        mismatches.append((name, wanted_version, installed_version))
    print(f'  {name:<20} installed={installed_version:<12} wanted={wanted_version}{marker}')

print()
print('=' * 70)
print('send_file() SIGNATURE CHECK (Flask/Werkzeug version drift)')
print('=' * 70)
try:
    import flask
    import inspect
    params = list(inspect.signature(flask.send_file).parameters.keys())
    print('flask.send_file() parameters:', params)
    if 'download_name' in params:
        print('  OK -- this Flask version supports download_name= (what the app\'s code uses).')
    else:
        print('  *** PROBLEM FOUND ***')
        print('  This Flask version does NOT support download_name=.')
        print('  Every Import/Export route in the app calls send_file(..., download_name=...),')
        print('  so EVERY one of them will crash with:')
        print("    TypeError: send_file() got an unexpected keyword argument 'download_name'")
        print('  Fix: reinstall requirements.txt into this exact virtualenv (see mismatches above),')
        print('  then restart the app from cPanel.')
except Exception as e:
    print('  Could not import flask / check send_file():', repr(e))

print()
print('=' * 70)
print('OPENPYXL WRITE TEST (in-memory only, nothing saved to disk)')
print('=' * 70)
try:
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws['A1'] = 'test'
    buf = io.BytesIO()
    wb.save(buf)
    print(f'  OK -- built a test .xlsx in memory ({buf.tell()} bytes). openpyxl itself works fine.')
except Exception as e:
    print('  *** PROBLEM FOUND -- openpyxl failed to build a file ***')
    print('  ', repr(e))

print()
print('=' * 70)
print('SUMMARY')
print('=' * 70)
if missing:
    print(f'  Missing packages: {", ".join(missing)}')
if mismatches:
    print('  Version mismatches (installed != requirements.txt):')
    for name, wanted_v, got_v in mismatches:
        print(f'    - {name}: installed {got_v}, requirements.txt wants {wanted_v}')
if not missing and not mismatches:
    print('  All requirements.txt packages match what is actually installed.')
    print('  If Import/Export still 500s, the real traceback is in cPanel\'s')
    print('  error log / Passenger log for this app -- please paste that instead.')
print()
print('Copy everything above this line and send it back.')
