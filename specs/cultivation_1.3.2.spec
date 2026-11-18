# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['../P1Clicker.py'],
    pathex=[],
    binaries=[],
    datas=[('../PNGpicture/' + name + '.png', 'PNGpicture') for name in
           ('Dj_2up', 'Dj_2_c', 'Dj_up', 'Dj_P', 'P1', 'P2-caption', 'store', 'upgrade',
            'swort_in_bt', 'sword_up_bt', 'sword_exit')] +
          [('../P1Clicker.ico', '.'), ('../P1Clicker.png', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='cultivation_1.3.2',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['../P1Clicker.ico'],
)
