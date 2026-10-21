# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apple Silicon .app. The window and the portal are the same process as Windows."""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

sys.path.insert(0, os.path.abspath(os.path.join(SPECPATH, os.pardir)))
from app import __version__

root = os.path.abspath(os.path.join(SPECPATH, os.pardir))

hidden = [
    "yaml",
    "multipart",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]
hidden += collect_submodules("webview")
if sys.platform == "darwin":
    hidden += collect_submodules("objc")
    hidden += collect_submodules("WebKit")

a = Analysis(
    [os.path.join(root, "macos", "__main__.py")],
    pathex=[root],
    binaries=[],
    datas=[(os.path.join(root, "app", "static"), os.path.join("app", "static"))],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["mock_jamf", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ipad-tech-portal",
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ipad-tech-portal",
)
app = BUNDLE(
    coll,
    name="iPad Tech Portal.app",
    icon=None,
    bundle_identifier="org.ipad-tech-portal",
    version=__version__,
    info_plist={
        "CFBundleName": "iPad Tech Portal",
        "CFBundleDisplayName": "iPad Tech Portal",
        "CFBundleIdentifier": "org.ipad-tech-portal",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "26.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Copyright (C) 2026 Marvin Alonso",
        "NSAppTransportSecurity": {
            "NSAllowsArbitraryLoads": False,
            "NSAllowsLocalNetworking": True,
        },
    },
)
