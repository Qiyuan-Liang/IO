# -*- mode: python ; coding: utf-8 -*-

import glob
import os
import sys


BASE_DIR = os.path.abspath(globals().get("SPECPATH", os.getcwd()))
SCRIPT_PATH = os.path.join(BASE_DIR, "camera_GUI.py")
ICON_PATH = os.path.join(BASE_DIR, "icon.ico")


def _collect_files(src_dir, pattern, dest_dir, out_list):
    if not os.path.isdir(src_dir):
        return
    for p in glob.glob(os.path.join(src_dir, pattern)):
        if os.path.isfile(p):
            out_list.append((p, dest_dir))


datas = []
binaries = []

# Keep MVS Python wrappers in app so target PC only needs installed MVS runtime/driver.
mvs_import_dir = r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport"
_collect_files(mvs_import_dir, "*.py", "MvImport", datas)

# Conda Python may need ffi*.dll for _ctypes when frozen.
conda_bin_dir = os.path.join(sys.prefix, "Library", "bin")
_collect_files(conda_bin_dir, "ffi*.dll", ".", binaries)


a = Analysis(
    [SCRIPT_PATH],
    pathex=[BASE_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=["cv2", "numpy"],
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
    [],
    exclude_binaries=True,
    name="camera_GUI",
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
    icon=ICON_PATH if os.path.isfile(ICON_PATH) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="camera_GUI_easy",
)
