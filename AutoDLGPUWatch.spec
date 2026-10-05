# -*- mode: python ; coding: utf-8 -*-
"""Native onedir build. User configuration and credentials are never collected."""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH)

analysis = Analysis(
    [str(ROOT / "desktop_launcher.pyw")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=copy_metadata("websocket-client") + [
        (str(ROOT / "src" / "autodl_gpu_watch" / "assets" / "app.png"), "autodl_gpu_watch/assets"),
        (str(ROOT / "src" / "autodl_gpu_watch" / "assets" / "app.ico"), "autodl_gpu_watch/assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="AutoDL Aoao",
    icon=str(ROOT / "assets" / "app.ico") if sys.platform == "win32" else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory="_internal",
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="AutoDL Aoao",
)
if sys.platform == "darwin":
    app = BUNDLE(
        collection,
        name="AutoDL Aoao.app",
        icon=str(ROOT / "assets" / "app.icns"),
        bundle_identifier="org.autodlgpuwatch.desktop",
        info_plist={
            "CFBundleDisplayName": "AutoDL Aoao",
            "NSHighResolutionCapable": True,
        },
    )
