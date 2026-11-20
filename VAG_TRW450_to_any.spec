# -*- mode: python ; coding: utf-8 -*-
# Сборка exe идёт через build_exe.bat (flet pack). Этот spec — запасной путь PyInstaller.
from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    ["abs_editor.py"],
    pathex=[str(root)],
    binaries=[],
    datas=[("assets/app.ico", "assets"), ("HELP.md", ".")],
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
    name="VAG_TRW450_to_any",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "assets" / "app.ico"),
)
