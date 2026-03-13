# -*- mode: python ; coding: utf-8 -*-

import glob
import os
import sys


BASE_DIR = os.path.abspath(globals().get("SPECPATH", os.getcwd()))
SCRIPT_PATH = os.path.join(BASE_DIR, "camera_GUI.py")


def _collect_files_from_dirs(src_dirs, pattern, dest_dir, target_list):
    seen = set()
    for src_dir in src_dirs:
        if not os.path.isdir(src_dir):
            continue
        for p in glob.glob(os.path.join(src_dir, pattern)):
            if not os.path.isfile(p):
                continue
            key = (os.path.normcase(os.path.abspath(p)), dest_dir)
            if key in seen:
                continue
            seen.add(key)
            target_list.append((p, dest_dir))


def _collect_tree_files(src_dirs, relative_subdir, pattern, target_list):
    seen = set()
    for src_root in src_dirs:
        root = os.path.join(src_root, relative_subdir)
        if not os.path.isdir(root):
            continue
        for p in glob.glob(os.path.join(root, pattern)):
            if not os.path.isfile(p):
                continue
            rel_name = os.path.basename(p)
            key = (os.path.normcase(os.path.abspath(p)), rel_name)
            if key in seen:
                continue
            seen.add(key)
            target_list.append((p, os.path.join(relative_subdir, rel_name)))


def _collect_tree_recursive(src_dirs, dest_root, target_list):
    seen = set()
    for src_root in src_dirs:
        if not os.path.isdir(src_root):
            continue
        for root, _, files in os.walk(src_root):
            for file_name in files:
                src_path = os.path.join(root, file_name)
                rel = os.path.relpath(src_path, src_root)
                dest_dir = os.path.join(dest_root, os.path.dirname(rel)).replace("\\", "/")
                key = (os.path.normcase(os.path.abspath(src_path)), dest_dir)
                if key in seen:
                    continue
                seen.add(key)
                target_list.append((src_path, dest_dir))


mvs_import_dirs = [
    r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport",
]

mvs_runtime_win64_dirs = [
    r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64",
    r"C:\Program Files (x86)\MVS\Runtime\Win64_x64",
]

mvs_runtime_win32_dirs = [
    r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win32_i86",
    r"C:\Program Files (x86)\MVS\Runtime\Win32_i86",
]

mvs_dev_lib_win64_dirs = [
    r"C:\Program Files (x86)\MVS\Development\Libraries\win64",
]

mvs_dev_lib_win32_dirs = [
    r"C:\Program Files (x86)\MVS\Development\Libraries\win32",
]

python_runtime_dirs = [
    os.path.join(sys.prefix, "Library", "bin"),
]


datas = []
binaries = []

# Bundle MVS Python wrapper modules into MvImport/.
_collect_files_from_dirs(mvs_import_dirs, "*.py", "MvImport", datas)

# Bundle full runtime trees (DLL/CTI/INI/manifests/ThirdParty files).
_collect_tree_recursive(mvs_runtime_win64_dirs, "MVS/Runtime/Win64_x64", binaries)
_collect_tree_recursive(mvs_runtime_win32_dirs, "MVS/Runtime/Win32_i86", binaries)

# Also include development libs as additional fallback lookup locations.
_collect_files_from_dirs(mvs_dev_lib_win64_dirs, "*.dll", "MVS/Development/Libraries/win64", binaries)
_collect_files_from_dirs(mvs_dev_lib_win32_dirs, "*.dll", "MVS/Development/Libraries/win32", binaries)

# Conda builds may require ffi*.dll for Python _ctypes at runtime.
_collect_files_from_dirs(python_runtime_dirs, "ffi*.dll", ".", binaries)


a = Analysis(
    [SCRIPT_PATH],
    pathex=[BASE_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "cv2",
        "numpy",
        "MvCameraControl_class",
        "MvErrorDefine_const",
        "CameraParams_header",
        "CameraParams_const",
        "PixelType_header",
    ],
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="camera_GUI_portable",
)
