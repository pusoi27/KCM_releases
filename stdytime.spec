# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Stdytime v04.05.16

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ['launcher.py'],  # entry point: tray icon + Waitress
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('assets', 'assets'),
        ('data', 'data'),
    ],
    hiddenimports=[
        'flask',
        'werkzeug',
        'reportlab',
        *collect_submodules('reportlab.graphics.barcode'),
        'sqlite3',
        'waitress',
        'pystray',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'modules.database',
        'modules.student_manager',
        'modules.timer_manager',
        'modules.qr_generator',
        'modules.assistant_manager',
        'modules.reports',
        'modules.utils',
        'modules.license_manager',
        'modules.ls_license',
        'routes.dashboard',
        'routes.students',
        'routes.assistants',
        'routes.api',
        'routes.qr',
        'routes.reports',
        'routes.license',
        'routes.auth',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludedimports=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Stdytime',
    icon='assets/stdytime.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # show a console window for logs/process visibility
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
