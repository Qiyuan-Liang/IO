# -*- mode: python ; coding: utf-8 -*-

import glob
import os


def _collect_files(src_dir, pattern, dest_dir, target_list):
    if not os.path.isdir(src_dir):
        return
    for p in glob.glob(os.path.join(src_dir, pattern)):
        if os.path.isfile(p):
            target_list.append((p, dest_dir))


mvs_import_dir = r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport"
mvs_runtime_win64 = r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64"
mvs_runtime_win32 = r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win32_i86"

datas = []
binaries = []

_collect_files(mvs_import_dir, "*.py", "MvImport", datas)
_collect_files(mvs_runtime_win64, "*.dll", "MVS/Runtime/Win64_x64", binaries)
_collect_files(mvs_runtime_win32, "*.dll", "MVS/Runtime/Win32_i86", binaries)


a = Analysis(
    ['camera_GUI.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=['cv2', 'numpy', 'MvCameraControl_class', 'MvErrorDefine_const', 'CameraParams_header', 'CameraParams_const', 'PixelType_header'],
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
    name='camera_GUI',
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
)
