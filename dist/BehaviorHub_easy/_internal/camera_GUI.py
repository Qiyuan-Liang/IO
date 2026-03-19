import importlib
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

import cv2
import numpy as np
try:
    import tifffile  # type: ignore[import-not-found]
except Exception:
    tifffile = None
from PyQt6.QtCore import QObject, QPoint, QRect, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

nidaqmx = None


def _decode_char(ctypes_char_array) -> str:
    raw = memoryview(ctypes_char_array).tobytes()
    null_index = raw.find(b"\x00")
    if null_index != -1:
        raw = raw[:null_index]
    for encoding in ("gbk", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1", errors="replace")


def _append_mvs_import_path():
    candidates = []

    runenv = os.getenv("MVCAM_COMMON_RUNENV", "").strip()
    if runenv:
        candidates.append(os.path.join(runenv, "Samples", "Python", "MvImport"))

    # Common default install path on Windows.
    candidates.append(r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport")

    # If bundled with PyInstaller, try paths inside the extraction dir.
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(os.path.join(meipass, "MvImport"))
        candidates.append(os.path.join(meipass, "MVS", "Development", "Samples", "Python", "MvImport"))

    for candidate in candidates:
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.append(candidate)


def _configure_mvs_dll_paths():
    if not hasattr(os, "add_dll_directory"):
        return

    dll_dirs = []
    runenv = os.getenv("MVCAM_COMMON_RUNENV", "").strip()
    if runenv:
        dll_dirs.extend(
            [
                runenv,
                os.path.join(runenv, "Runtime", "Win64_x64"),
                os.path.join(runenv, "Runtime", "Win32_i86"),
            ]
        )

    dll_dirs.extend(
        [
            r"C:\Program Files (x86)\MVS\Runtime\Win64_x64",
            r"C:\Program Files (x86)\MVS\Runtime\Win32_i86",
            r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64",
            r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win32_i86",
            r"C:\Program Files (x86)\MVS\Development\Libraries\win64",
            r"C:\Program Files (x86)\MVS\Development\Libraries\win32",
        ]
    )

    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        dll_dirs.extend(
            [
                meipass,
                os.path.join(meipass, "MVS", "Runtime", "Win64_x64"),
                os.path.join(meipass, "MVS", "Runtime", "Win32_i86"),
            ]
        )

    for d in dll_dirs:
        try:
            if os.path.isdir(d):
                os.add_dll_directory(d)
        except Exception:
            pass


def _append_env_path_var(var_name: str, path_value: str):
    if not path_value or not os.path.isdir(path_value):
        return
    existing = os.getenv(var_name, "")
    parts = [p for p in existing.split(os.pathsep) if p]
    norm_target = os.path.normcase(os.path.abspath(path_value))
    norm_parts = {os.path.normcase(os.path.abspath(p)) for p in parts if os.path.isdir(p)}
    if norm_target in norm_parts:
        return
    parts.append(path_value)
    os.environ[var_name] = os.pathsep.join(parts)


def _configure_mvs_runtime_env():
    # Make GenTL producers discoverable; prefer machine-installed MVS runtime.
    gentl_dirs = []
    runenv = os.getenv("MVCAM_COMMON_RUNENV", "").strip()
    if runenv and os.path.isdir(runenv):
        gentl_dirs.extend(
            [
                os.path.join(runenv, "Runtime", "Win64_x64"),
                os.path.join(runenv, "Runtime", "Win32_i86"),
            ]
        )

    common_runtime_win64 = r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64"
    common_runtime_win32 = r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win32_i86"
    mvs_runtime_win64 = r"C:\Program Files (x86)\MVS\Runtime\Win64_x64"
    mvs_runtime_win32 = r"C:\Program Files (x86)\MVS\Runtime\Win32_i86"
    gentl_dirs.extend([common_runtime_win64, common_runtime_win32, mvs_runtime_win64, mvs_runtime_win32])

    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        bundled_win64 = os.path.join(meipass, "MVS", "Runtime", "Win64_x64")
        bundled_win32 = os.path.join(meipass, "MVS", "Runtime", "Win32_i86")
        if os.path.isdir(bundled_win64):
            gentl_dirs.append(bundled_win64)
        if os.path.isdir(bundled_win32):
            gentl_dirs.append(bundled_win32)

    for p in gentl_dirs:
        _append_env_path_var("GENICAM_GENTL64_PATH", p)
        _append_env_path_var("GENICAM_GENTL32_PATH", p)


@dataclass
class FramePacket:
    frame: np.ndarray
    ts_s: float
    frame_id: Optional[int] = None
    is_mono: bool = False


@dataclass
class SaveConfig:
    folder: str
    base_name: str
    file_ext: str
    fourcc_candidates: List[str]
    fps: float
    queue_size: int
    drop_if_full: bool


class AsyncVideoWriter:
    def __init__(
        self,
        out_path: str,
        fourcc_candidates: List[str],
        fps: float,
        frame_size: Tuple[int, int],
        queue_size: int,
        prefer_mono: bool,
    ):
        self.selected_fourcc = None
        self._writer = None
        self.is_color = not bool(prefer_mono)
        for fourcc in fourcc_candidates:
            codec = cv2.VideoWriter_fourcc(*fourcc)
            w = cv2.VideoWriter(out_path, codec, max(1.0, float(fps)), frame_size, self.is_color)
            if w.isOpened():
                self._writer = w
                self.selected_fourcc = fourcc
                break
        # Fallback: if mono writer is unsupported for the chosen codec/container, retry color mode.
        if self._writer is None and prefer_mono:
            self.is_color = True
            for fourcc in fourcc_candidates:
                codec = cv2.VideoWriter_fourcc(*fourcc)
                w = cv2.VideoWriter(out_path, codec, max(1.0, float(fps)), frame_size, True)
                if w.isOpened():
                    self._writer = w
                    self.selected_fourcc = fourcc
                    break
        if self._writer is None:
            attempted = ", ".join(fourcc_candidates)
            raise RuntimeError(f"Failed to create writer: {out_path}. Tried codecs: {attempted}")

        self._q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(maxsize=max(8, int(queue_size)))
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.frames_written = 0
        self.frames_dropped = 0
        self._thread.start()

    def _run(self):
        while not self._stop:
            item = self._q.get()
            if item is None:
                break
            self._writer.write(item)
            self.frames_written += 1
        # Drain any leftover frames before closing.
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            self._writer.write(item)
            self.frames_written += 1
        self._writer.release()

    def submit(self, frame_bgr: np.ndarray, drop_if_full: bool) -> bool:
        if drop_if_full:
            try:
                self._q.put_nowait(frame_bgr)
                return True
            except queue.Full:
                self.frames_dropped += 1
                return False
        self._q.put(frame_bgr)
        return True

    def queue_state(self) -> Tuple[int, int]:
        return (self._q.qsize(), self._q.maxsize)

    def close(self):
        self._stop = True
        try:
            self._q.put_nowait(None)
        except queue.Full:
            # Ensure sentinel is accepted.
            self._q.put(None)
        self._thread.join(timeout=5.0)


class AsyncTiffWriter:
    def __init__(self, out_path: str, queue_size: int):
        if tifffile is None:
            raise RuntimeError("tifffile is not installed. Install package 'tifffile' to enable multi-frame TIFF recording.")
        self.selected_fourcc = "TIFF"
        self._out_path = out_path
        self.is_color = False
        self._q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(maxsize=max(8, int(queue_size)))
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.frames_written = 0
        self.frames_dropped = 0
        self._thread.start()

    def _run(self):
        with tifffile.TiffWriter(self._out_path, bigtiff=True) as tw:
            while not self._stop:
                item = self._q.get()
                if item is None:
                    break
                tw.write(item, contiguous=True)
                self.frames_written += 1
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    continue
                tw.write(item, contiguous=True)
                self.frames_written += 1

    def submit(self, frame: np.ndarray, drop_if_full: bool) -> bool:
        if drop_if_full:
            try:
                self._q.put_nowait(frame)
                return True
            except queue.Full:
                self.frames_dropped += 1
                return False
        self._q.put(frame)
        return True

    def queue_state(self) -> Tuple[int, int]:
        return (self._q.qsize(), self._q.maxsize)

    def close(self):
        self._stop = True
        try:
            self._q.put_nowait(None)
        except queue.Full:
            self._q.put(None)
        self._thread.join(timeout=8.0)


class CameraBackend:
    def list_devices(self):
        raise NotImplementedError

    def open(self, index: int):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def start_grabbing(self):
        raise NotImplementedError

    def stop_grabbing(self):
        raise NotImplementedError

    def get_frame(self, timeout_ms: int = 1000) -> Optional[FramePacket]:
        raise NotImplementedError

    def set_exposure_us(self, exposure_us: float):
        raise NotImplementedError

    def set_gain(self, gain: float):
        raise NotImplementedError

    def set_frame_rate(self, fps: float):
        raise NotImplementedError

    def set_throughput_limit(self, bps: int):
        raise NotImplementedError

    def set_resolution(self, width: int, height: int):
        raise NotImplementedError

    def get_gain_range(self) -> Optional[Tuple[float, float, float]]:
        return None

    def get_resolution_range(self) -> Optional[Tuple[int, int, int, int, int, int]]:
        return None

    def apply_stream_buffering(self, image_node_num: int, grab_strategy: int, output_queue_size: int):
        _ = image_node_num
        _ = grab_strategy
        _ = output_queue_size

    def configure_sync_output(self, enabled: bool, source_mode: str, pulse_duration_us: float, output_line: str):
        _ = enabled
        _ = source_mode
        _ = pulse_duration_us
        _ = output_line

    def get_throughput_status(self) -> Optional[str]:
        return None

    def configure_record_trigger(self, enabled: bool, source_line: str):
        _ = enabled
        _ = source_line

    def get_line_state(self, source_line: str) -> Optional[int]:
        _ = source_line
        return None


class OpenCVCameraBackend(CameraBackend):
    def __init__(self):
        self.cap = None
        self._frame_counter = 0

    def list_devices(self):
        devices = []
        for i in range(6):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                devices.append((i, f"OpenCV Camera {i}"))
                cap.release()
        return devices

    def open(self, index: int):
        self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            raise RuntimeError(f"Failed to open camera index {index}")

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def start_grabbing(self):
        return

    def stop_grabbing(self):
        return

    def get_frame(self, timeout_ms: int = 1000) -> Optional[FramePacket]:
        _ = timeout_ms
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        if not ok:
            return None
        self._frame_counter += 1
        return FramePacket(frame=frame, ts_s=time.time(), frame_id=self._frame_counter, is_mono=(frame.ndim == 2))

    def set_exposure_us(self, exposure_us: float):
        if self.cap is not None:
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
            self.cap.set(cv2.CAP_PROP_EXPOSURE, exposure_us)

    def set_gain(self, gain: float):
        if self.cap is not None:
            self.cap.set(cv2.CAP_PROP_GAIN, gain)

    def set_frame_rate(self, fps: float):
        if self.cap is not None:
            self.cap.set(cv2.CAP_PROP_FPS, fps)

    def set_throughput_limit(self, bps: int):
        _ = bps

    def set_resolution(self, width: int, height: int):
        if self.cap is not None:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    def get_gain_range(self) -> Optional[Tuple[float, float, float]]:
        return (0.0, 30.0, 0.0)

    def get_resolution_range(self) -> Optional[Tuple[int, int, int, int, int, int]]:
        return (320, 1920, 1, 240, 1200, 1)


class HikMVSCameraBackend(CameraBackend):
    def __init__(self):
        self._sdk_ok = False
        self._sdk_initialized = False
        self._mvs = None
        self._cam = None
        self._st_device_info = None
        self._supports_tput = False
        self._is_grabbing = False
        self._init_error = ""
        self._last_tput_status = "Throughput node unavailable"

        _append_mvs_import_path()
        _configure_mvs_dll_paths()
        _configure_mvs_runtime_env()

        try:
            self._mvs = importlib.import_module("MvCameraControl_class")
            ret = self._mvs.MvCamera.MV_CC_Initialize()
            if ret == 0:
                self._sdk_initialized = True
                self._sdk_ok = True
            else:
                self._init_error = f"MV_CC_Initialize failed: 0x{ret:08X}"
        except Exception:
            self._sdk_ok = False
            self._init_error = str(sys.exc_info()[1])

    def is_available(self) -> bool:
        return self._sdk_ok

    def get_init_error(self) -> str:
        return self._init_error

    def _require_sdk(self):
        if not self._sdk_ok or self._mvs is None:
            raise RuntimeError(
                "Hikrobot MVS SDK is not ready. Ensure MVCAM_COMMON_RUNENV is set and "
                "MvCameraControl_class.py is available."
            )

    def _tlayer_mask(self) -> int:
        m = self._mvs
        return (
            int(getattr(m, "MV_GIGE_DEVICE", 0))
            | int(getattr(m, "MV_USB_DEVICE", 0))
            | int(getattr(m, "MV_GENTL_GIGE_DEVICE", 0))
            | int(getattr(m, "MV_GENTL_CAMERALINK_DEVICE", 0))
            | int(getattr(m, "MV_GENTL_CXP_DEVICE", 0))
            | int(getattr(m, "MV_GENTL_XOF_DEVICE", 0))
        )

    def _enum_device_list(self):
        self._require_sdk()
        m = self._mvs
        device_list = m.MV_CC_DEVICE_INFO_LIST()
        ret = m.MvCamera.MV_CC_EnumDevices(self._tlayer_mask(), device_list)
        if ret != 0:
            raise RuntimeError(f"MV_CC_EnumDevices failed: 0x{ret:08X}")
        return device_list

    def list_devices(self):
        m = self._mvs
        device_list = self._enum_device_list()
        devices = []
        for i in range(device_list.nDeviceNum):
            info = m.cast(device_list.pDeviceInfo[i], m.POINTER(m.MV_CC_DEVICE_INFO)).contents
            name = f"Hikrobot Device {i}"
            t = int(info.nTLayerType)
            if t in {int(getattr(m, "MV_USB_DEVICE", -1))}:
                model = _decode_char(info.SpecialInfo.stUsb3VInfo.chModelName)
                serial = _decode_char(info.SpecialInfo.stUsb3VInfo.chSerialNumber)
                name = f"USB3 {model} ({serial})"
            elif t in {
                int(getattr(m, "MV_GIGE_DEVICE", -1)),
                int(getattr(m, "MV_GENTL_GIGE_DEVICE", -1)),
            }:
                model = _decode_char(info.SpecialInfo.stGigEInfo.chModelName)
                name = f"GigE {model}"
            devices.append((i, name))
        return devices

    def open(self, index: int):
        m = self._mvs
        if self._cam is not None:
            self.close()

        device_list = self._enum_device_list()
        if index < 0 or index >= device_list.nDeviceNum:
            raise RuntimeError("Selected camera index is out of range.")

        self._cam = m.MvCamera()
        self._st_device_info = m.cast(device_list.pDeviceInfo[index], m.POINTER(m.MV_CC_DEVICE_INFO)).contents

        ret = self._cam.MV_CC_CreateHandle(self._st_device_info)
        if ret != 0:
            self._cam = None
            raise RuntimeError(f"MV_CC_CreateHandle failed: 0x{ret:08X}")

        ret = self._cam.MV_CC_OpenDevice(m.MV_ACCESS_Exclusive, 0)
        if ret != 0:
            self._cam.MV_CC_DestroyHandle()
            self._cam = None
            raise RuntimeError(f"MV_CC_OpenDevice failed: 0x{ret:08X}")

        # Match official samples for GigE packet setup.
        if int(self._st_device_info.nTLayerType) in {
            int(getattr(m, "MV_GIGE_DEVICE", -1)),
            int(getattr(m, "MV_GENTL_GIGE_DEVICE", -1)),
        }:
            packet_size = self._cam.MV_CC_GetOptimalPacketSize()
            if int(packet_size) > 0:
                self._cam.MV_CC_SetIntValue("GevSCPSPacketSize", packet_size)

        # Continuous grabbing mode by default.
        self._safe_set_enum("TriggerMode", int(m.MV_TRIGGER_MODE_OFF))
        self._safe_set_enum("AcquisitionMode", 2)

        # Probe throughput node support once.
        self._supports_tput = self._safe_set_int("DeviceLinkThroughputLimit", 120000000)

    def close(self):
        if self._cam is not None:
            try:
                self._cam.MV_CC_StopGrabbing()
            except Exception:
                pass
            try:
                self._cam.MV_CC_CloseDevice()
            except Exception:
                pass
            try:
                self._cam.MV_CC_DestroyHandle()
            except Exception:
                pass
        self._cam = None
        self._st_device_info = None

    def finalize(self):
        self.close()
        if self._sdk_initialized and self._mvs is not None:
            try:
                self._mvs.MvCamera.MV_CC_Finalize()
            except Exception:
                pass
            self._sdk_initialized = False

    def start_grabbing(self):
        if self._cam is None:
            raise RuntimeError("Camera is not opened.")
        ret = self._cam.MV_CC_StartGrabbing()
        if ret != 0:
            raise RuntimeError(f"MV_CC_StartGrabbing failed: 0x{ret:08X}")
        self._is_grabbing = True

    def stop_grabbing(self):
        if self._cam is None:
            return
        self._cam.MV_CC_StopGrabbing()
        self._is_grabbing = False

    def _safe_set_enum(self, key: str, value: int) -> bool:
        if self._cam is None:
            return False
        ret = self._cam.MV_CC_SetEnumValue(key, int(value))
        return ret == 0

    def _safe_set_enum_str(self, key: str, value: str) -> bool:
        if self._cam is None:
            return False
        ret = self._cam.MV_CC_SetEnumValueByString(key, value)
        return ret == 0

    def _safe_set_float(self, key: str, value: float) -> bool:
        if self._cam is None:
            return False
        ret = self._cam.MV_CC_SetFloatValue(key, float(value))
        return ret == 0

    def _safe_set_int(self, key: str, value: int) -> bool:
        if self._cam is None:
            return False
        ret = self._cam.MV_CC_SetIntValue(key, int(value))
        return ret == 0

    def _get_int_info(self, key: str):
        if self._cam is None:
            return None
        m = self._mvs
        st_val = m.MVCC_INTVALUE()
        m.memset(m.byref(st_val), 0, m.sizeof(st_val))
        ret = self._cam.MV_CC_GetIntValue(key, st_val)
        if ret != 0:
            return None
        return {
            "cur": int(st_val.nCurValue),
            "min": int(st_val.nMin),
            "max": int(st_val.nMax),
            "inc": max(1, int(st_val.nInc)),
        }

    def _get_float_info(self, key: str):
        if self._cam is None:
            return None
        m = self._mvs
        st_val = m.MVCC_FLOATVALUE()
        m.memset(m.byref(st_val), 0, m.sizeof(st_val))
        ret = self._cam.MV_CC_GetFloatValue(key, st_val)
        if ret != 0:
            return None
        return {
            "cur": float(st_val.fCurValue),
            "min": float(st_val.fMin),
            "max": float(st_val.fMax),
        }

    @staticmethod
    def _align_to_inc(value: int, min_v: int, inc: int) -> int:
        if inc <= 1:
            return value
        return min_v + ((value - min_v) // inc) * inc

    def get_frame(self, timeout_ms: int = 1000) -> Optional[FramePacket]:
        if self._cam is None:
            return None
        m = self._mvs

        st_out = m.MV_FRAME_OUT()
        m.memset(m.byref(st_out), 0, m.sizeof(st_out))

        ret = self._cam.MV_CC_GetImageBuffer(st_out, int(timeout_ms))
        if ret != 0 or st_out.pBufAddr is None:
            return None

        try:
            width = int(st_out.stFrameInfo.nWidth)
            height = int(st_out.stFrameInfo.nHeight)
            if width <= 0 or height <= 0:
                return None

            frame_id = int(st_out.stFrameInfo.nFrameNum)

            # First try Mono8 conversion for monochrome cameras to avoid color-cast artifacts in encoded video.
            mono8 = int(getattr(m, "PixelType_Gvsp_Mono8", 0))
            if mono8:
                mono_size = width * height
                mono_param = m.MV_CC_PIXEL_CONVERT_PARAM_EX()
                m.memset(m.byref(mono_param), 0, m.sizeof(mono_param))
                mono_param.nWidth = width
                mono_param.nHeight = height
                mono_param.pSrcData = st_out.pBufAddr
                mono_param.nSrcDataLen = st_out.stFrameInfo.nFrameLen
                mono_param.enSrcPixelType = st_out.stFrameInfo.enPixelType
                mono_param.enDstPixelType = mono8
                mono_param.pDstBuffer = (m.c_ubyte * mono_size)()
                mono_param.nDstBufferSize = mono_size
                mono_ret = self._cam.MV_CC_ConvertPixelTypeEx(mono_param)
                if mono_ret == 0 and int(mono_param.nDstLen) >= mono_size:
                    mono_arr = np.ctypeslib.as_array(mono_param.pDstBuffer, shape=(mono_size,))
                    frame_mono = mono_arr.reshape(height, width).copy()
                    return FramePacket(frame=frame_mono, ts_s=time.time(), frame_id=frame_id, is_mono=True)

            rgb_size = width * height * 3
            convert_param = m.MV_CC_PIXEL_CONVERT_PARAM_EX()
            m.memset(m.byref(convert_param), 0, m.sizeof(convert_param))
            convert_param.nWidth = width
            convert_param.nHeight = height
            convert_param.pSrcData = st_out.pBufAddr
            convert_param.nSrcDataLen = st_out.stFrameInfo.nFrameLen
            convert_param.enSrcPixelType = st_out.stFrameInfo.enPixelType
            convert_param.enDstPixelType = m.PixelType_Gvsp_RGB8_Packed
            convert_param.pDstBuffer = (m.c_ubyte * rgb_size)()
            convert_param.nDstBufferSize = rgb_size
            ret = self._cam.MV_CC_ConvertPixelTypeEx(convert_param)
            if ret != 0:
                return None

            arr = np.ctypeslib.as_array(convert_param.pDstBuffer, shape=(convert_param.nDstLen,))
            frame_rgb = arr.reshape(height, width, 3).copy()
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            return FramePacket(frame=frame_bgr, ts_s=time.time(), frame_id=frame_id, is_mono=False)
        finally:
            self._cam.MV_CC_FreeImageBuffer(st_out)

    def set_exposure_us(self, exposure_us: float):
        # 0 is often "Off" for ExposureAuto in GenICam nodes.
        self._safe_set_enum("ExposureAuto", 0)
        self._safe_set_float("ExposureTime", exposure_us)

    def set_gain(self, gain: float):
        self._safe_set_enum("GainAuto", 0)
        self._safe_set_float("Gain", gain)

    def set_frame_rate(self, fps: float):
        # On Hik cameras this node is typically boolean; enum writes may be ignored.
        self._safe_set_bool("AcquisitionFrameRateEnable", True)
        ok = self._safe_set_float("AcquisitionFrameRate", fps)
        if not ok:
            raise RuntimeError("Failed to set AcquisitionFrameRate")

    def set_throughput_limit(self, bps: int):
        target_bps = max(1_000_000, int(bps))
        # Mode token differs across cameras (enum or bool on some models).
        self._safe_set_enum("DeviceLinkThroughputLimitMode", 1)
        self._safe_set_enum_str("DeviceLinkThroughputLimitMode", "On")
        self._safe_set_bool("DeviceLinkThroughputLimitMode", True)

        info = self._get_int_info("DeviceLinkThroughputLimit")
        if info is not None:
            target_bps = max(info["min"], min(info["max"], target_bps))

        ok = self._safe_set_int("DeviceLinkThroughputLimit", target_bps)
        # For GigE cameras, force minimum inter-packet delay for max available link throughput.
        self._safe_set_int("GevSCPD", 0)

        cur_info = self._get_int_info("DeviceLinkThroughputLimit")
        if cur_info is not None:
            cur = int(cur_info["cur"])
            max_v = int(cur_info["max"])
            mode = "ON" if ok else "UNKNOWN"
            self._last_tput_status = f"Throughput {mode}: {cur} bps (max {max_v})"
        elif ok:
            self._last_tput_status = f"Throughput set to {target_bps} bps"
        else:
            self._last_tput_status = "Throughput node rejected value"

    def get_throughput_status(self) -> Optional[str]:
        return self._last_tput_status

    def set_resolution(self, width: int, height: int):
        w_info = self._get_int_info("Width")
        h_info = self._get_int_info("Height")
        if w_info is None or h_info is None:
            self._safe_set_int("Width", width)
            self._safe_set_int("Height", height)
            return

        target_w = max(w_info["min"], min(w_info["max"], int(width)))
        target_h = max(h_info["min"], min(h_info["max"], int(height)))
        target_w = self._align_to_inc(target_w, w_info["min"], w_info["inc"])
        target_h = self._align_to_inc(target_h, h_info["min"], h_info["inc"])

        was_grabbing = self._is_grabbing
        if was_grabbing:
            self.stop_grabbing()

        # Many cameras require offset reset before changing ROI size.
        self._safe_set_int("OffsetX", 0)
        self._safe_set_int("OffsetY", 0)
        ok_w = self._safe_set_int("Width", target_w)
        ok_h = self._safe_set_int("Height", target_h)

        if was_grabbing:
            self.start_grabbing()

        if not (ok_w and ok_h):
            raise RuntimeError("Resolution node rejected current value. Check width/height increment constraints.")

    def get_gain_range(self) -> Optional[Tuple[float, float, float]]:
        info = self._get_float_info("Gain")
        if info is None:
            return None
        return (info["min"], info["max"], info["cur"])

    def get_resolution_range(self) -> Optional[Tuple[int, int, int, int, int, int]]:
        w = self._get_int_info("Width")
        h = self._get_int_info("Height")
        if w is None or h is None:
            return None
        return (w["min"], w["max"], w["inc"], h["min"], h["max"], h["inc"])

    def apply_stream_buffering(self, image_node_num: int, grab_strategy: int, output_queue_size: int):
        if self._cam is None:
            return
        node_num = max(1, int(image_node_num))
        queue_size = max(1, int(output_queue_size))
        strategy = int(grab_strategy)
        self._cam.MV_CC_SetImageNodeNum(node_num)
        self._cam.MV_CC_SetGrabStrategy(strategy)
        self._cam.MV_CC_SetOutputQueueSize(queue_size)

    def configure_sync_output(self, enabled: bool, source_mode: str, pulse_duration_us: float, output_line: str) -> Optional[str]:
        """Configure strobe/sync pulse output matching MVS software flow.

        Stops grabbing before configuring, then restarts — many Hikrobot USB3
        cameras only latch strobe registers on stream start.
        MVS order: LineSelector -> LineSource -> StrobeDuration -> StrobeEnable.
        """
        if self._cam is None:
            return None

        # Stop grabbing so register writes take effect on next start.
        was_grabbing = self._is_grabbing
        if was_grabbing:
            self.stop_grabbing()

        try:
            return self._apply_strobe_registers(enabled, source_mode, pulse_duration_us, output_line)
        finally:
            if was_grabbing:
                self.start_grabbing()

    def _apply_strobe_registers(self, enabled: bool, source_mode: str, pulse_duration_us: float, output_line: str) -> Optional[str]:
        line_name = (output_line or "Line1").strip()
        if not self._safe_set_enum_str("LineSelector", line_name):
            raise RuntimeError(f"Sync output failed: unsupported LineSelector '{line_name}'")

        if not enabled:
            self._safe_set_bool("StrobeEnable", False)
            self._safe_set_enum_str("LineSource", "Off")
            return "disabled"

        # Try Strobe line mode (some models need it; others ignore it).
        self._safe_set_enum_str("LineMode", "Strobe")

        # --- LineSource ---
        # GenICam strings vary across firmware; try every plausible alias.
        source_map = {
            "Start of exposure": ["ExposureStartActive", "ExposureStart", "FrameStartActive", "FrameActive", "FrameStart"],
            "Start of frame":   ["FrameStartActive", "FrameActive", "FrameStart", "ExposureStartActive", "ExposureStart"],
            "Frame start active": ["FrameStartActive", "FrameActive", "FrameBurstStartActive", "FrameStart", "ExposureStartActive"],
            "End of frame":     ["FrameEnd", "ExposureEnd"],
            "Exposure":         ["ExposureActive", "ExposureStartActive", "ExposureStart"],
        }
        candidates = source_map.get(source_mode, ["FrameStartActive", "FrameActive", "FrameStart"])
        applied_token = None
        for token in candidates:
            if self._safe_set_enum_str("LineSource", token):
                applied_token = token
                break
        if applied_token is None:
            raise RuntimeError(
                f"Sync output failed: no supported LineSource for mode '{source_mode}' on {line_name}"
            )

        # --- StrobeDuration -> StrobeEnable (MVS order) ---
        self._safe_set_float("StrobeDuration", float(max(1.0, pulse_duration_us)))
        self._safe_set_bool("StrobeEnable", True)
        return f"{line_name}:{applied_token}"

    def configure_record_trigger(self, enabled: bool, source_line: str):
        if self._cam is None:
            return
        # Keep camera in free-run; OPTO input is used as an external gate for start/stop logic.
        self._safe_set_enum_str("LineSelector", source_line)
        self._safe_set_enum_str("LineMode", "Input")
        self._safe_set_enum("TriggerMode", int(getattr(self._mvs, "MV_TRIGGER_MODE_OFF", 0)))

    def get_line_state(self, source_line: str) -> Optional[int]:
        if self._cam is None:
            return None
        try:
            self._safe_set_enum_str("LineSelector", source_line)
            val = self._mvs.c_bool(False)
            ret = self._cam.MV_CC_GetBoolValue("LineStatus", val)
            if ret != 0:
                return None
            return 1 if bool(val.value) else 0
        except Exception:
            return None

    def _safe_set_bool(self, key: str, value: bool) -> bool:
        if self._cam is None:
            return False
        ret = self._cam.MV_CC_SetBoolValue(key, bool(value))
        return ret == 0


class CameraWorker(QObject):
    frame_ready = pyqtSignal(object)
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    connected = pyqtSignal(bool)
    recording_state = pyqtSignal(bool)
    limits_ready = pyqtSignal(object)
    camera_fps = pyqtSignal(float)
    buffer_stats = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.backend: Optional[CameraBackend] = None
        self._lock = threading.Lock()
        self._stop_loop = False
        self._live = False
        self._record_armed = False
        self._recording = False
        self._save_cfg: Optional[SaveConfig] = None
        self._writer = None
        self._snapshot_path = None
        self._trigger_enabled = True
        self._stop_by_trigger = True
        self._armed_on_frame_id = None
        self._record_started_on_frame_id = None
        self._last_frame_id = None
        self._last_frame_ts = None
        self._preview_target_fps = 60.0
        self._requested_fps = 60.0
        self._last_preview_emit_ts = 0.0
        self._fps_accum_frames = 0
        self._fps_accum_time = 0.0
        self._latest_camera_fps = None
        self._record_start_ts = None
        self._record_first_frame_ts = None
        self._record_last_frame_ts = None
        self._record_frame_count = 0
        self._record_path = None
        self._record_writer_fps = None
        self._record_queue_drops = 0
        self._record_started = False
        self._trigger_source_line = "Line0"
        self._last_trigger_level = None
        self._auto_start_enabled = False
        self._min_stop_trigger_delay_s = 0.05
        self._cam_image_node_num = 32
        self._cam_output_queue_size = 8
        self._last_stats_emit_ts = 0.0
        self._output_resolution = None  # (w, h) target for full-frame downsampling
        self._roi_rect = None  # (x, y, w, h) in raw-frame pixels
        self._trigger_poll_interval_s = 0.010  # poll trigger level at most every 10ms
        self._last_trigger_poll_ts = 0.0
        self._last_res_report = None
        self._shared_frame_counter = None

    def set_backend(self, backend: CameraBackend):
        self.backend = backend

    def set_roi_rect(self, roi_rect: Optional[Tuple[int, int, int, int]]):
        with self._lock:
            if roi_rect is None:
                self._roi_rect = None
                self._last_res_report = None
                self.status.emit("ROI cleared (full frame)")
                return
            x, y, w, h = [int(v) for v in roi_rect]
            if w < 2 or h < 2:
                return
            self._roi_rect = (x, y, w, h)
            self._last_res_report = None
            self.status.emit(f"ROI set: x={x}, y={y}, w={w}, h={h}")

    def _apply_roi_zoom(self, frame: np.ndarray) -> Tuple[np.ndarray, Optional[Tuple[int, int, int, int]]]:
        with self._lock:
            roi_rect = self._roi_rect
        if roi_rect is None:
            return frame, None

        raw_h, raw_w = frame.shape[:2]
        x, y, w, h = roi_rect
        x = max(0, min(x, raw_w - 1))
        y = max(0, min(y, raw_h - 1))
        w = max(1, min(w, raw_w - x))
        h = max(1, min(h, raw_h - y))
        if w <= 1 or h <= 1:
            return frame, None

        crop = frame[y : y + h, x : x + w]
        if crop.shape[0] <= 1 or crop.shape[1] <= 1:
            return frame, None

        if crop.shape[1] == raw_w and crop.shape[0] == raw_h:
            return frame, (x, y, w, h)

        # Keep ROI aspect ratio: scale-to-fit into full frame and pad with black margins.
        scale = min(raw_w / max(1, w), raw_h / max(1, h))
        fit_w = max(1, int(round(w * scale)))
        fit_h = max(1, int(round(h * scale)))
        resized = cv2.resize(crop, (fit_w, fit_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.zeros_like(frame)
        off_x = max(0, (raw_w - fit_w) // 2)
        off_y = max(0, (raw_h - fit_h) // 2)
        canvas[off_y : off_y + fit_h, off_x : off_x + fit_w] = resized
        return canvas, (x, y, w, h)

    def _apply_output_downsample(self, frame: np.ndarray) -> np.ndarray:
        with self._lock:
            target = self._output_resolution
        if target is None:
            return frame
        target_w, target_h = int(target[0]), int(target[1])
        src_h, src_w = frame.shape[:2]
        if target_w <= 0 or target_h <= 0:
            return frame
        # Never upscale; only downsample whole frame to avoid sensor top-left cropping behavior.
        if target_w >= src_w or target_h >= src_h:
            return frame
        return cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)

    def _emit_resolution_report(self, raw_frame: np.ndarray, out_frame: np.ndarray, roi_rect: Optional[Tuple[int, int, int, int]]):
        raw_h, raw_w = raw_frame.shape[:2]
        out_h, out_w = out_frame.shape[:2]
        sig = (raw_w, raw_h, out_w, out_h, roi_rect)
        if sig == self._last_res_report:
            return
        self._last_res_report = sig

        if roi_rect is None:
            self.status.emit(f"Real resolution: output={out_w}x{out_h}, raw={raw_w}x{raw_h}, ROI=full")
            return

        _, _, rw, rh = roi_rect
        zoom_x = (raw_w / rw) if rw > 0 else 1.0
        zoom_y = (raw_h / rh) if rh > 0 else 1.0
        self.status.emit(
            f"Real resolution: output={out_w}x{out_h}, raw={raw_w}x{raw_h}, ROI={rw}x{rh}, zoom={zoom_x:.2f}x/{zoom_y:.2f}x"
        )

    def list_devices(self):
        if self.backend is None:
            return []
        return self.backend.list_devices()

    def connect_camera(self, index: int):
        if self.backend is None:
            raise RuntimeError("No camera backend initialized.")
        self.backend.open(index)
        self.backend.start_grabbing()
        self.connected.emit(True)
        self.status.emit("Camera connected and grabbing")

    def disconnect_camera(self):
        with self._lock:
            self._live = False
            self._record_armed = False
            self._recording = False
        self._release_writer()
        if self.backend is not None:
            try:
                self.backend.stop_grabbing()
            except Exception:
                pass
            self.backend.close()
        self.connected.emit(False)
        self.status.emit("Camera disconnected")

    def apply_settings(self, exposure_us: float, fps: float, gain: float, resolution: Tuple[int, int]):
        if self.backend is None:
            return
        self.backend.set_exposure_us(exposure_us)
        self.backend.set_frame_rate(fps)
        self.backend.set_gain(gain)
        with self._lock:
            self._output_resolution = (int(resolution[0]), int(resolution[1]))
        self.status.emit(
            f"Applied: Exp={exposure_us:.1f}us, FPS={fps:.2f}, Gain={gain:.2f}, Res={resolution[0]}x{resolution[1]}"
        )

    def set_frame_rate_only(self, fps: float):
        if self.backend is None:
            return
        self.backend.set_frame_rate(fps)
        self.status.emit(f"Applied frame rate: {fps:.2f} Hz")

    def set_exposure_only(self, exposure_us: float):
        if self.backend is None:
            return
        self.backend.set_exposure_us(exposure_us)
        self.status.emit(f"Applied exposure: {exposure_us:.1f} us")

    def set_gain_only(self, gain: float):
        if self.backend is None:
            return
        self.backend.set_gain(gain)
        self.status.emit(f"Applied gain: {gain:.2f}")

    def set_resolution_only(self, width: int, height: int):
        with self._lock:
            self._output_resolution = (int(width), int(height))
        self.status.emit(f"Applied resolution: {int(width)}x{int(height)}")

    def apply_sync_output(self, enabled: bool, source_mode: str, pulse_duration_us: float, output_line: str):
        if self.backend is None:
            return
        applied_source = self.backend.configure_sync_output(enabled, source_mode, pulse_duration_us, output_line)
        source_text = applied_source if applied_source is not None else "n/a"
        msg = f"Sync output: enabled={enabled}, line={output_line}, mode={source_mode}, applied_source={source_text}, pulse={pulse_duration_us:.1f}us"
        if msg != getattr(self, '_last_sync_log', None):
            self._last_sync_log = msg
            self.status.emit(msg)

    def apply_record_trigger(self, enabled: bool, source_line: str, _silent: bool = False):
        if self.backend is None:
            return
        self._trigger_source_line = source_line
        self.backend.configure_record_trigger(enabled, source_line)
        if not _silent:
            self.status.emit(f"Record trigger: enabled={enabled}, source={source_line}")

    def _read_trigger_level(self) -> Optional[int]:
        if self.backend is None:
            return None
        level = self.backend.get_line_state(self._trigger_source_line)
        if level is None:
            return None
        self._last_trigger_level = level
        return int(level)

    def apply_buffer_settings(self, image_node_num: int, grab_strategy: int, output_queue_size: int):
        if self.backend is None:
            return
        self._cam_image_node_num = int(image_node_num)
        self._cam_output_queue_size = int(output_queue_size)
        self.backend.apply_stream_buffering(image_node_num, grab_strategy, output_queue_size)
        self.status.emit(
            f"Applied buffering: ImageNodeNum={image_node_num}, Strategy={grab_strategy}, OutputQueueSize={output_queue_size}"
        )

    def query_camera_limits(self):
        if self.backend is None:
            return
        gain_range = self.backend.get_gain_range()
        res_range = self.backend.get_resolution_range()
        self.limits_ready.emit({"gain_range": gain_range, "res_range": res_range})

    def start_live(self):
        with self._lock:
            self._live = True

    def stop_live(self):
        with self._lock:
            self._live = False

    def arm_recording(self, save_cfg: SaveConfig, trigger_enabled: bool, stop_by_trigger: bool, auto_start: bool = False):
        with self._lock:
            self._save_cfg = save_cfg
            self._trigger_enabled = bool(trigger_enabled)
            self._stop_by_trigger = bool(stop_by_trigger)
            self._auto_start_enabled = bool(auto_start)
            self._record_armed = True
            self._recording = False
            self._armed_on_frame_id = self._last_frame_id
            self._record_started_on_frame_id = None
            self._record_started = False
            self._last_trigger_level = None

        if trigger_enabled:
            self.status.emit("Acquisition armed; waiting for trigger HIGH level")
        else:
            self.status.emit("Acquisition armed")

    def set_auto_start_mode(self, enabled: bool, save_cfg: SaveConfig):
        with self._lock:
            self._auto_start_enabled = bool(enabled)
            self._save_cfg = save_cfg
            if enabled:
                self._trigger_enabled = True
                self._stop_by_trigger = True
            self._last_trigger_level = None

    def set_preview_target_fps(self, requested_fps: float):
        requested = max(1.0, float(requested_fps))
        self._requested_fps = requested
        self._preview_target_fps = requested if requested <= 60.0 else 60.0

    def _release_writer(self):
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self.recording_state.emit(False)

    def stop_recording(self, send_end_pulse: bool = False):
        with self._lock:
            was_recording = self._recording or self._record_armed
            self._recording = False
            self._record_armed = False
            started = self._record_started
        if was_recording:
            summary = None
            if self._record_start_ts is not None:
                wall_duration_s = max(0.0, time.time() - self._record_start_ts)
                active_duration_s = None
                capture_fps = None
                if (
                    self._record_first_frame_ts is not None
                    and self._record_last_frame_ts is not None
                    and self._record_frame_count > 1
                ):
                    active_duration_s = max(0.0, self._record_last_frame_ts - self._record_first_frame_ts)
                    if active_duration_s > 0:
                        capture_fps = (self._record_frame_count - 1) / active_duration_s

                writer_fps = self._record_writer_fps if self._record_writer_fps is not None else 0.0
                if capture_fps is None:
                    summary = (
                        f"Recording summary: wall={wall_duration_s:.3f}s, frames={self._record_frame_count}, "
                        f"writer_fps={writer_fps:.3f}, queue_drop={self._record_queue_drops}"
                    )
                else:
                    summary = (
                        f"Recording summary: wall={wall_duration_s:.3f}s, active={active_duration_s:.3f}s, "
                        f"frames={self._record_frame_count}, capture_fps={capture_fps:.3f}, "
                        f"writer_fps={writer_fps:.3f}, queue_drop={self._record_queue_drops}"
                    )
            self._release_writer()
            if started:
                self.status.emit("Recording stopped")
            else:
                self.status.emit("Acquisition stopped before start trigger; no recording file created")
            if summary is not None:
                path_msg = f", file={self._record_path}" if self._record_path else ""
                self.status.emit(summary + path_msg)
            self._record_start_ts = None
            self._record_first_frame_ts = None
            self._record_last_frame_ts = None
            self._record_frame_count = 0
            self._record_path = None
            self._record_writer_fps = None
            self._record_queue_drops = 0
            self._record_started = False

    def snapshot(self, path: str):
        self._snapshot_path = path

    def _build_writer(self, frame_shape, cfg: SaveConfig):
        h, w = frame_shape[:2]
        clip_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        out_path = os.path.join(cfg.folder, f"{cfg.base_name}_{clip_id}.{cfg.file_ext}")
        ext = (cfg.file_ext or "").strip().lower()

        if ext in {"tif", "tiff"}:
            writer = AsyncTiffWriter(out_path=out_path, queue_size=cfg.queue_size)
            self.status.emit(f"Writer format: multi-frame TIFF | Queue: {cfg.queue_size}")
            return writer, out_path

        # Align file timeline to actual measured capture cadence when available.
        write_fps = float(cfg.fps)
        if self._latest_camera_fps is not None and self._latest_camera_fps > 0:
            write_fps = float(self._latest_camera_fps)
        writer = AsyncVideoWriter(
            out_path=out_path,
            fourcc_candidates=cfg.fourcc_candidates,
            fps=max(1.0, write_fps),
            frame_size=(w, h),
            queue_size=cfg.queue_size,
            prefer_mono=bool(getattr(self, "_record_is_mono", False)),
        )
        mode = "mono" if not writer.is_color else "color"
        self.status.emit(f"Writer FPS set to {max(1.0, write_fps):.3f} | Codec: {writer.selected_fourcc} | Mode: {mode}")
        return writer, out_path

    def run(self):
        while True:
            with self._lock:
                if self._stop_loop:
                    break
                live = self._live
                armed = self._record_armed
                recording = self._recording
                trigger_enabled = self._trigger_enabled
                stop_by_trigger = self._stop_by_trigger
                auto_start = self._auto_start_enabled
                save_cfg = self._save_cfg

            trigger_level = None
            if trigger_enabled and (armed or recording or auto_start):
                now_t = time.time()
                if (now_t - self._last_trigger_poll_ts) >= self._trigger_poll_interval_s:
                    trigger_level = self._read_trigger_level()
                    self._last_trigger_poll_ts = now_t
                else:
                    trigger_level = self._last_trigger_level

            # Auto-start mode: whenever trigger is HIGH start a clip; when LOW stop current clip.
            if auto_start and trigger_enabled and (not recording) and trigger_level == 1:
                with self._lock:
                    self._recording = True
                    self._record_armed = False
                    self._record_started_on_frame_id = self._last_frame_id
                    self._record_started = True
                self._record_start_ts = time.time()
                self._record_first_frame_ts = None
                self._record_last_frame_ts = None
                self._record_frame_count = 0
                if self._shared_frame_counter is not None:
                    self._shared_frame_counter.value = 0
                self._record_writer_fps = None
                self._record_queue_drops = 0
                self._record_is_mono = False
                self.status.emit("Auto-start: trigger HIGH, recording started")
                recording = True
                armed = False

            if armed and trigger_enabled and trigger_level == 1:
                self.status.emit("Trigger level HIGH detected; recording started")
                with self._lock:
                    if self._record_armed:
                        self._recording = True
                        self._record_armed = False
                        self._record_started_on_frame_id = self._last_frame_id
                        self._record_started = True
                self._record_start_ts = time.time()
                self._record_first_frame_ts = None
                self._record_last_frame_ts = None
                self._record_frame_count = 0
                if self._shared_frame_counter is not None:
                    self._shared_frame_counter.value = 0
                self._record_writer_fps = None
                self._record_queue_drops = 0
                self._record_is_mono = False
                # Refresh state after transition.
                recording = True
                armed = False

            if armed and (not trigger_enabled):
                with self._lock:
                    if self._record_armed:
                        self._recording = True
                        self._record_armed = False
                        self._record_started_on_frame_id = self._last_frame_id
                        self._record_started = True
                self._record_start_ts = time.time()
                self._record_first_frame_ts = None
                self._record_last_frame_ts = None
                self._record_frame_count = 0
                if self._shared_frame_counter is not None:
                    self._shared_frame_counter.value = 0
                self._record_writer_fps = None
                self._record_queue_drops = 0
                self._record_is_mono = False
                self.status.emit("Trigger-arm disabled; recording started immediately")
                recording = True
                armed = False

            if recording and stop_by_trigger and trigger_enabled and trigger_level == 0:
                if self._record_start_ts is not None and (time.time() - self._record_start_ts) < self._min_stop_trigger_delay_s:
                    continue
                if auto_start:
                    self.status.emit("Auto-start: trigger LOW, recording stopped")
                else:
                    self.status.emit("Trigger level LOW detected; recording stopped")
                self.stop_recording(send_end_pulse=False)
                continue

            if self.backend is None or (not live and not armed and not recording and (not auto_start) and self._snapshot_path is None):
                time.sleep(0.002)
                continue

            packet = self.backend.get_frame(timeout_ms=50)
            if packet is None:
                continue

            raw_frame = packet.frame
            scaled_frame = self._apply_output_downsample(raw_frame)
            frame, roi_rect = self._apply_roi_zoom(scaled_frame)
            self._emit_resolution_report(raw_frame, frame, roi_rect)

            if packet.frame_id is not None and self._last_frame_id is not None and self._last_frame_ts is not None:
                frame_delta = int(packet.frame_id) - int(self._last_frame_id)
                time_delta = float(packet.ts_s) - float(self._last_frame_ts)
                if frame_delta > 0 and time_delta > 0:
                    self._fps_accum_frames += frame_delta
                    self._fps_accum_time += time_delta
                    if self._fps_accum_time >= 0.5:
                        measured = self._fps_accum_frames / self._fps_accum_time
                        self._latest_camera_fps = measured
                        self.camera_fps.emit(measured)
                        self._fps_accum_frames = 0
                        self._fps_accum_time = 0.0
            self._last_frame_id = packet.frame_id
            self._last_frame_ts = packet.ts_s

            should_emit_preview = True
            if self._requested_fps > 60.0:
                min_interval = 1.0 / max(1.0, self._preview_target_fps)
                if (packet.ts_s - self._last_preview_emit_ts) < min_interval:
                    should_emit_preview = False

            if should_emit_preview:
                self.frame_ready.emit(frame)
                self._last_preview_emit_ts = packet.ts_s

            if self._snapshot_path is not None:
                try:
                    cv2.imwrite(self._snapshot_path, frame)
                    self.status.emit(f"Snapshot saved: {self._snapshot_path}")
                except Exception as exc:
                    self.error.emit(f"Snapshot failed: {exc}")
                self._snapshot_path = None

            with self._lock:
                recording = self._recording

            if recording:
                if self._writer is None:
                    if save_cfg is None:
                        self.error.emit("Missing save config")
                        self.stop_recording(send_end_pulse=False)
                        continue
                    try:
                        os.makedirs(save_cfg.folder, exist_ok=True)
                        self._writer, out = self._build_writer(frame.shape, save_cfg)
                        self._record_writer_fps = float(max(1.0, self._latest_camera_fps if (self._latest_camera_fps is not None and self._latest_camera_fps > 0) else save_cfg.fps))
                        self._record_path = out
                        self.recording_state.emit(True)
                        self.status.emit(f"Recording: {out}")
                    except Exception as exc:
                        self.error.emit(str(exc))
                        self.stop_recording(send_end_pulse=False)
                        continue
                try:
                    if self._record_frame_count == 0:
                        self._record_is_mono = bool(packet.is_mono or frame.ndim == 2)
                    frame_to_write = frame
                    if frame_to_write.ndim == 2 and getattr(self._writer, "is_color", True):
                        frame_to_write = cv2.cvtColor(frame_to_write, cv2.COLOR_GRAY2BGR)
                    accepted = self._writer.submit(frame_to_write, save_cfg.drop_if_full)
                    if not accepted:
                        self._record_queue_drops += 1
                    self._record_frame_count += 1
                    if self._shared_frame_counter is not None:
                        self._shared_frame_counter.value = self._record_frame_count
                    if self._record_first_frame_ts is None:
                        self._record_first_frame_ts = packet.ts_s
                    self._record_last_frame_ts = packet.ts_s
                except Exception as exc:
                    self.error.emit(f"Writer error, stopping recording: {exc}")
                    self.stop_recording(send_end_pulse=False)
                    continue
                # stop-by-trigger is handled by line-edge polling above.

            now = time.time()
            if (now - self._last_stats_emit_ts) >= 0.25:
                sw_used = 0
                sw_max = 0
                if self._writer is not None:
                    sw_used, sw_max = self._writer.queue_state()
                self.buffer_stats.emit(
                    {
                        "cam_node": self._cam_image_node_num,
                        "cam_outq": self._cam_output_queue_size,
                        "sw_used": sw_used,
                        "sw_max": sw_max,
                        "drops": self._record_queue_drops,
                    }
                )
                self._last_stats_emit_ts = now

    def shutdown(self):
        with self._lock:
            self._stop_loop = True
            self._live = False
            self._record_armed = False
            self._recording = False
        self._release_writer()
        if self.backend is not None:
            try:
                self.backend.stop_grabbing()
            except Exception:
                pass


class PreviewLabel(QLabel):
    roi_drawn = pyqtSignal(object)
    roi_cleared = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dragging = False
        self._roi_select_enabled = False
        self._drag_start = QPoint()
        self._drag_rect = None
        self._roi_overlay_rect = None

    def set_roi_select_enabled(self, enabled: bool):
        self._roi_select_enabled = bool(enabled)
        if not self._roi_select_enabled:
            self._dragging = False
            self._drag_rect = None
        self.update()

    def is_roi_select_enabled(self) -> bool:
        return self._roi_select_enabled

    def set_roi_overlay_rect(self, rect: Optional[QRect]):
        self._roi_overlay_rect = QRect(rect) if rect is not None else None
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton and self._roi_select_enabled:
            self._dragging = False
            self._drag_rect = None
            self.roi_cleared.emit()
            self.update()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._roi_select_enabled:
            self._dragging = True
            self._drag_start = event.position().toPoint()
            self._drag_rect = QRect(self._drag_start, self._drag_start)
            self.update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            cur = event.position().toPoint()
            self._drag_rect = QRect(self._drag_start, cur).normalized()
            self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == Qt.MouseButton.LeftButton and self._roi_select_enabled:
            self._dragging = False
            final_rect = QRect(self._drag_rect).normalized() if self._drag_rect is not None else QRect()
            self._drag_rect = None
            if final_rect.width() >= 6 and final_rect.height() >= 6:
                self.roi_drawn.emit(final_rect)
            self.update()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._roi_overlay_rect is not None:
            painter.setPen(QPen(QColor(0, 255, 120), 2, Qt.PenStyle.SolidLine))
            painter.drawRect(self._roi_overlay_rect)

        if self._drag_rect is not None:
            painter.setPen(QPen(QColor(255, 215, 0), 2, Qt.PenStyle.DashLine))
            painter.drawRect(self._drag_rect)

        if self._roi_select_enabled and self._drag_rect is None:
            painter.setPen(QPen(QColor(255, 180, 0), 1, Qt.PenStyle.DotLine))
            painter.drawRect(self.rect().adjusted(2, 2, -2, -2))


class CameraGUI(QMainWindow):
    def __init__(self, module_mode: bool = False, hub_mode: bool = False):
        super().__init__()
        self._module_mode = module_mode
        self._hub_mode = hub_mode
        self._module_log_lines: list = []
        self._module_save_folder: str = os.getcwd()
        self._module_base_name: str = "camera_run"
        self.setWindowTitle("Camera Module" if module_mode else "HKrobot Camera GUI")
        self.resize(1000, 600)

        self.worker = CameraWorker()
        self.worker_thread = QThread()
        self._connected = False
        self._backend_obj = None
        self._display_map = None  # (x0, y0, disp_w, disp_h, frame_w, frame_h)
        self._roi_frame_rect = None
        self._record_indicator_on = False
        self._is_recording_now = False
        self._record_flash_timer = QTimer(self)
        self._record_flash_timer.setInterval(320)
        self._record_flash_timer.timeout.connect(self._toggle_record_indicator)
        self._shared_frame_counter = None

        self._init_ui()
        self._bind_signals()
        self._init_backend()

        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker_thread.start()
        QTimer.singleShot(250, self._auto_connect_on_startup)

    def set_shared_frame_counter(self, counter) -> None:
        """Set a multiprocessing.Value shared with the master process."""
        self._shared_frame_counter = counter
        self.worker._shared_frame_counter = counter

    def _auto_connect_on_startup(self):
        if self._connected:
            return
        if self.device_combo.count() <= 0:
            return
        self.log("Attempting auto-connect on startup")
        self.on_connect()

    def _init_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        # Unified top control bar.
        top = QHBoxLayout()
        self.backend_label = QLabel("Source: -")
        self.device_combo = QComboBox()
        self.refresh_btn = QPushButton("Refresh")
        self.live_start_btn = QPushButton("Start live")
        self.live_stop_btn = QPushButton("Stop live")
        self.live_stop_btn.hide()
        self.snapshot_btn = QPushButton("Snapshot")
        self.snapshot_btn.setEnabled(False)
        self.acq_start_btn = QPushButton("Start acquisition")
        self.acq_stop_btn = QPushButton("Stop acquisition")
        self.acq_stop_btn.hide()
        self.rec_indicator_btn = QPushButton("")
        self.rec_indicator_btn.setEnabled(False)
        self.rec_indicator_btn.setFixedSize(18, 18)
        self.rec_indicator_btn.setToolTip("Recording indicator")

        top.addWidget(self.backend_label)
        top.addWidget(QLabel("Camera"))
        top.addWidget(self.device_combo, 1)
        top.addWidget(self.refresh_btn)
        top.addSpacing(8)
        top.addWidget(self.live_start_btn)
        top.addWidget(self.live_stop_btn)
        top.addWidget(self.snapshot_btn)
        top.addWidget(self.acq_start_btn)
        top.addWidget(self.acq_stop_btn)
        top.addWidget(self.rec_indicator_btn)
        layout.addLayout(top)
        self._set_record_indicator_idle()

        # Left panel as tabs to save space.
        self.left_tabs = QTabWidget()
        self.left_tabs.setMinimumWidth(292)
        self.left_tabs.setMaximumWidth(345)

        # Camera tab
        camera_tab = QWidget()
        camera_form = QFormLayout(camera_tab)

        self.exposure_spin = QDoubleSpinBox()
        self.exposure_spin.setRange(10.0, 200000.0)
        self.exposure_spin.setDecimals(1)
        self.exposure_spin.setValue(10000.0)

        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(1.0, 500.0)
        self.fps_spin.setDecimals(2)
        self.fps_spin.setValue(60.0)

        self.gain_spin = QDoubleSpinBox()
        self.gain_spin.setRange(0.0, 30.0)
        self.gain_spin.setDecimals(2)
        self.gain_spin.setValue(10.0)
        self.gain_range_label = QLabel("Max: -- dB")
        gain_row = QHBoxLayout()
        gain_row.setContentsMargins(0, 0, 0, 0)
        gain_row.addWidget(self.gain_spin)
        gain_row.addWidget(self.gain_range_label)
        gain_row.addStretch(1)
        gain_wrap = QWidget()
        gain_wrap.setLayout(gain_row)

        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems([
            "1440x1080",
            "1280x1024",
            "1024x768",
            "800x600",
            "640x480",
        ])
        self.roi_select_btn = QPushButton("Select ROI")
        self.roi_select_btn.setCheckable(True)
        self.roi_clear_btn = QPushButton("Clear ROI")
        roi_row = QHBoxLayout()
        roi_row.setContentsMargins(0, 0, 0, 0)
        roi_row.addWidget(self.roi_select_btn)
        roi_row.addWidget(self.roi_clear_btn)
        roi_wrap = QWidget()
        roi_wrap.setLayout(roi_row)


        camera_form.addRow("Exposure (us)", self.exposure_spin)
        camera_form.addRow("Frame rate (Hz)", self.fps_spin)
        camera_form.addRow("Gain", gain_wrap)
        camera_form.addRow("Resolution", self.resolution_combo)
        camera_form.addRow("ROI", roi_wrap)

        # Save tab
        save_tab = QWidget()
        save_form = QFormLayout(save_tab)
        self.save_folder_edit = QLineEdit(os.getcwd())
        self.save_browse_btn = QPushButton("Browse")
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.save_folder_edit)
        folder_row.addWidget(self.save_browse_btn)
        folder_wrap = QWidget()
        folder_wrap.setLayout(folder_row)

        self.base_name_edit = QLineEdit("camera_run")
        self.codec_profile_combo = QComboBox()
        self.codec_profile_combo.addItem("AVI fast (MJPG with fallbacks)", ("avi", ["MJPG", "XVID", "DIVX"]))
        self.codec_profile_combo.addItem("AVI compact (XVID with fallbacks)", ("avi", ["XVID", "DIVX", "MJPG"]))
        self.codec_profile_combo.addItem("MP4 compact (mp4v with fallbacks)", ("mp4", ["mp4v", "avc1", "H264"]))
        self.codec_profile_combo.addItem("TIFF stack (multi-frame, lossless)", ("tiff", []))
        self.codec_profile_combo.setCurrentIndex(0)

        self.queue_size_spin = QSpinBox()
        self.queue_size_spin.setRange(8, 8192)
        self.queue_size_spin.setValue(512)
        queue_row = QHBoxLayout()
        queue_row.setContentsMargins(0, 0, 0, 0)
        queue_row.addWidget(self.queue_size_spin)
        queue_row.addWidget(QLabel("frames"))
        queue_row.addStretch(1)
        queue_wrap = QWidget()
        queue_wrap.setLayout(queue_row)

        self.drop_frames_check = QCheckBox("Drop frames if writer queue is full")
        self.drop_frames_check.setChecked(True)

        save_form.addRow("Folder", folder_wrap)
        save_form.addRow("Base name", self.base_name_edit)
        save_form.addRow("Format", self.codec_profile_combo)
        save_form.addRow("Buffer", queue_wrap)
        save_form.addRow("", self.drop_frames_check)

        # Hardware trigger/sync tab
        ni_tab = QWidget()
        ni_form = QFormLayout(ni_tab)
        self.trigger_input_line_combo = QComboBox()
        self.trigger_input_line_combo.addItems(["Line0", "Line1", "Line2", "Line3"])
        self.trigger_input_line_combo.setCurrentText("Line0")

        self.trigger_enable_check = QCheckBox("Enable level trigger")
        self.trigger_enable_check.setChecked(False)
        self.auto_start_check = QCheckBox("Auto-start with level trigger")
        self.auto_start_check.setChecked(False)

        self.sync_enable_check = QCheckBox("Enable sync/strobe")
        self.sync_enable_check.setChecked(True)
        self.sync_mode_combo = QComboBox()
        self.sync_mode_combo.addItems(["Frame start active", "Start of frame", "Start of exposure", "End of frame", "Exposure"])
        self.sync_mode_combo.setCurrentText("Frame start active")
        self.sync_output_line_combo = QComboBox()
        self.sync_output_line_combo.addItems(["Line1", "Line2", "Line3"])
        self.sync_output_line_combo.setCurrentText("Line1")
        self.sync_pulse_us_spin = QDoubleSpinBox()
        self.sync_pulse_us_spin.setRange(1.0, 500000.0)
        self.sync_pulse_us_spin.setDecimals(1)
        self.sync_pulse_us_spin.setValue(1000.0)

        ni_form.addRow("Trigger line", self.trigger_input_line_combo)
        ni_form.addRow("", self.trigger_enable_check)
        ni_form.addRow("", self.auto_start_check)
        ni_form.addRow("", self.sync_enable_check)
        ni_form.addRow("Sync output line", self.sync_output_line_combo)
        ni_form.addRow("Sync source", self.sync_mode_combo)
        ni_form.addRow("Pulse width (us)", self.sync_pulse_us_spin)

        self.grab_strategy_combo = QComboBox()
        self.grab_strategy_combo.addItems([
            "OneByOne (0)",
            "LatestImagesOnly (1)",
            "LatestImages (2)",
            "UpcomingImage (3)",
        ])
        self.grab_strategy_combo.setCurrentIndex(2)

        self.image_node_spin = QSpinBox()
        self.image_node_spin.setRange(1, 128)
        self.image_node_spin.setValue(32)

        self.output_queue_spin = QSpinBox()
        self.output_queue_spin.setRange(1, 128)
        self.output_queue_spin.setValue(8)

        camera_form.addRow("Grab strategy", self.grab_strategy_combo)
        camera_form.addRow("ImageNodeNum", self.image_node_spin)
        camera_form.addRow("OutputQueueSize", self.output_queue_spin)

        self.left_tabs.addTab(camera_tab, "Camera")
        self.left_tabs.addTab(save_tab, "Save")
        self.left_tabs.addTab(ni_tab, "I/O")

        # In hub mode: merge all settings into a single "Settings" tab,
        # hide save folder/name and trigger/sync (controlled by master),
        # and hide the log area.
        if self._hub_mode:
            # Remove the three default tabs
            while self.left_tabs.count():
                self.left_tabs.removeTab(0)

            # Build one merged tab
            merged_tab = QWidget()
            merged_form = QFormLayout(merged_tab)
            merged_form.addRow("Exposure (us)", self.exposure_spin)
            merged_form.addRow("Frame rate (Hz)", self.fps_spin)
            merged_form.addRow("Gain", gain_wrap)
            merged_form.addRow("Resolution", self.resolution_combo)
            merged_form.addRow("ROI", roi_wrap)
            merged_form.addRow("Grab strategy", self.grab_strategy_combo)
            merged_form.addRow("ImageNodeNum", self.image_node_spin)
            merged_form.addRow("OutputQueueSize", self.output_queue_spin)
            merged_form.addRow("Format", self.codec_profile_combo)
            merged_form.addRow("Buffer", queue_wrap)
            merged_form.addRow("", self.drop_frames_check)
            merged_form.addRow("Sync output line", self.sync_output_line_combo)
            merged_form.addRow("Sync source", self.sync_mode_combo)
            merged_form.addRow("Pulse width (us)", self.sync_pulse_us_spin)
            self.sync_enable_check.setVisible(False)
            self.left_tabs.addTab(merged_tab, "Settings")
        elif self._module_mode:
            # In module mode, hide save-path/name and trigger/sync settings
            # (controlled by the master BehaviorHub GUI).
            self.save_folder_edit.setVisible(False)
            self.save_browse_btn.setVisible(False)
            self.base_name_edit.setVisible(False)
            self.trigger_enable_check.setVisible(False)
            self.trigger_input_line_combo.setVisible(False)
            self.auto_start_check.setVisible(False)
            self.sync_enable_check.setVisible(False)
            self.left_tabs.setTabVisible(self.left_tabs.indexOf(ni_tab), False)

        self.sw_buf_label = QLabel("Buffer: 0/0")
        self.fps_label = QLabel("Camera FPS: --")
        left_bottom_row = QHBoxLayout()
        left_bottom_row.setContentsMargins(0, 4, 0, 0)
        left_bottom_row.addWidget(self.sw_buf_label)
        left_bottom_row.addStretch(1)
        left_bottom_row.addWidget(self.fps_label)

        left_panel = QVBoxLayout()
        left_panel.addWidget(self.left_tabs)
        left_panel.addLayout(left_bottom_row)
        left_wrap = QWidget()
        left_wrap.setLayout(left_panel)

        # Preview area
        self.preview_label = PreviewLabel("No frame")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("background:#111; color:#ddd; border:1px solid #444;")
        self.preview_label.setMinimumSize(740, 560)

        body = QHBoxLayout()
        body.addWidget(left_wrap)
        body.addWidget(self.preview_label, 1)
        layout.addLayout(body)

        self.status_box = QTextEdit()
        self.status_box.setReadOnly(True)
        self.status_box.setMinimumHeight(140)
        if self._hub_mode:
            self.status_box.setVisible(False)
        else:
            layout.addWidget(self.status_box)

    def _bind_signals(self):
        self.refresh_btn.clicked.connect(self.on_refresh_cycle)

        self.roi_select_btn.toggled.connect(self.on_roi_select_toggled)
        self.roi_clear_btn.clicked.connect(self.clear_roi)
        self.exposure_spin.valueChanged.connect(self._apply_exposure_live)
        self.gain_spin.valueChanged.connect(self._apply_gain_live)
        self.fps_spin.valueChanged.connect(self._apply_frame_rate_live)
        self.resolution_combo.currentTextChanged.connect(lambda _text: self._apply_resolution_live())
        self.grab_strategy_combo.currentIndexChanged.connect(lambda _idx: self.apply_buffer_settings())
        self.image_node_spin.valueChanged.connect(lambda _v: self.apply_buffer_settings())
        self.output_queue_spin.valueChanged.connect(lambda _v: self.apply_buffer_settings())

        self.live_start_btn.clicked.connect(self.start_live)
        self.live_stop_btn.clicked.connect(self.stop_live)
        self.snapshot_btn.clicked.connect(self.on_snapshot)
        self.acq_start_btn.clicked.connect(self.start_acquisition)
        self.acq_stop_btn.clicked.connect(self.stop_acquisition)

        self.save_browse_btn.clicked.connect(self.on_browse_folder)
        self.trigger_enable_check.toggled.connect(lambda _v: self.apply_trigger_settings())
        self.trigger_input_line_combo.currentTextChanged.connect(lambda _text: self.apply_trigger_settings())
        self.auto_start_check.toggled.connect(self.on_auto_start_toggled)
        self.sync_enable_check.toggled.connect(lambda _v: self.apply_sync_settings())
        self.sync_output_line_combo.currentTextChanged.connect(lambda _text: self.apply_sync_settings())
        self.sync_mode_combo.currentTextChanged.connect(lambda _text: self.apply_sync_settings())
        self.sync_pulse_us_spin.valueChanged.connect(lambda _val: self.apply_sync_settings())

        self.worker.frame_ready.connect(self.on_frame)
        self.worker.camera_fps.connect(self.on_camera_fps)
        self.worker.status.connect(lambda msg: self.log(msg))
        self.worker.error.connect(lambda msg: self.log(f"ERROR: {msg}"))
        self.worker.connected.connect(self.on_connected_state)
        self.worker.recording_state.connect(self.on_record_state)
        self.worker.limits_ready.connect(self.on_limits_ready)
        self.worker.buffer_stats.connect(self.on_buffer_stats)
        self.preview_label.roi_drawn.connect(self.on_preview_roi_drawn)
        self.preview_label.roi_cleared.connect(self.clear_roi)

    def _set_record_indicator_idle(self):
        self.rec_indicator_btn.hide()
        self.rec_indicator_btn.setStyleSheet("")

    def _set_record_indicator_flash(self, on: bool):
        self.rec_indicator_btn.show()
        if on:
            self.rec_indicator_btn.setStyleSheet(
                "QPushButton {background:#d90429; border:1px solid #9e0220; border-radius:9px;}"
            )
        else:
            self.rec_indicator_btn.setStyleSheet(
                "QPushButton {background:transparent; border:1px solid transparent; border-radius:9px;}"
            )

    def _toggle_record_indicator(self):
        if not self._is_recording_now:
            self._record_flash_timer.stop()
            self._set_record_indicator_idle()
            return
        self._record_indicator_on = not self._record_indicator_on
        self._set_record_indicator_flash(self._record_indicator_on)

    def _set_live_controls(self, live_running: bool):
        self.live_start_btn.setVisible(not live_running)
        self.live_stop_btn.setVisible(live_running)

    def _set_acq_controls(self, acquisition_running: bool):
        self.acq_start_btn.setVisible(not acquisition_running)
        self.acq_stop_btn.setVisible(acquisition_running)

    def _init_backend(self):
        hik_backend = HikMVSCameraBackend()
        if hik_backend.is_available():
            self._backend_obj = hik_backend
            self.worker.set_backend(hik_backend)
            self.backend_label.setText("Source: HikMVS")
            self.log("HikMVS initialized from official MVS Python API path")
        else:
            self._backend_obj = OpenCVCameraBackend()
            self.worker.set_backend(self._backend_obj)
            self.backend_label.setText("Source: OpenCV fallback")
            err = hik_backend.get_init_error().strip()
            if err:
                self.log(f"HikMVS unavailable: {err}")
            self.log("Fallback to OpenCV camera backend")
        self.refresh_devices()

    def on_refresh_cycle(self):
        if self._connected:
            self.on_disconnect()
        self.refresh_devices()
        if self.device_combo.count() <= 0:
            self.log("Refresh cycle: no camera found")
            return
        self.on_connect()
        if self._connected:
            self.log("Refresh cycle: connection check passed")
        else:
            self.log("Refresh cycle: connection check failed")

    def log(self, message: str):
        t = datetime.now().strftime("%H:%M:%S")
        self.status_box.append(f"[{t}] {message}")
        if self._module_mode:
            self._module_log_lines.append(message)

    def refresh_devices(self):
        self.device_combo.clear()
        try:
            devices = self.worker.list_devices()
        except Exception as exc:
            self.log(f"ERROR: List devices failed: {exc}")
            devices = []

        for idx, name in devices:
            self.device_combo.addItem(name, idx)

        if devices:
            self.log(f"Found {len(devices)} camera(s)")
        else:
            self.log("No camera found")

    def _selected_resolution(self) -> Tuple[int, int]:
        text = self.resolution_combo.currentText().strip().lower()
        if "x" not in text:
            return (640, 480)
        w, h = text.split("x")
        return int(w), int(h)

    def apply_trigger_settings(self, enable_override: Optional[bool] = None, _silent: bool = False):
        if not self._connected:
            return
        try:
            enabled = self.trigger_enable_check.isChecked() if enable_override is None else bool(enable_override)
            self.worker.apply_record_trigger(
                enabled=enabled,
                source_line=self.trigger_input_line_combo.currentText().strip(),
                _silent=_silent,
            )
        except Exception as exc:
            self.log(f"ERROR: Apply trigger settings failed: {exc}")

    def on_auto_start_toggled(self, enabled: bool):
        if enabled:
            self.trigger_enable_check.setChecked(True)
            self.trigger_enable_check.setEnabled(False)
            self._set_acq_controls(True)
        else:
            self.trigger_enable_check.setEnabled(True)
            self._set_acq_controls(False)

        if not self._connected:
            return

        try:
            self.worker.set_auto_start_mode(bool(enabled), self._build_save_cfg())
            self.apply_trigger_settings(
                enable_override=bool(enabled) or self.trigger_enable_check.isChecked(),
                _silent=True,
            )
            if enabled:
                self.worker.start_live()
                self._set_live_controls(True)
                self.log("Auto-start enabled")
            else:
                self.log("Auto-start disabled")
        except Exception as exc:
            self.log(f"ERROR: Auto-start update failed: {exc}")

    def on_connect(self):
        if self.device_combo.count() == 0:
            self.log("No device to connect")
            return
        idx = int(self.device_combo.currentData())
        try:
            self.worker.connect_camera(idx)
            self.apply_settings()
            self.apply_buffer_settings()
            # Keep free-run mode after connect so live FPS follows frame-rate settings.
            self.apply_trigger_settings(enable_override=False)
            # In module mode, the master pushes sync profile; skip initial apply.
            if not self._module_mode:
                self.apply_sync_settings()
            self.worker.query_camera_limits()
            if self.auto_start_check.isChecked():
                self.on_auto_start_toggled(True)
        except Exception as exc:
            self.log(f"ERROR: Connect failed: {exc}")

    def on_disconnect(self):
        try:
            self.worker.disconnect_camera()
            self.clear_roi(log_message=False)
        except Exception as exc:
            self.log(f"ERROR: Disconnect failed: {exc}")

    def apply_settings(self):
        if not self._connected:
            self.log("Settings cached; connect camera to apply")
            return
        try:
            sel_w, sel_h = self._selected_resolution()
            self.worker.apply_settings(
                exposure_us=float(self.exposure_spin.value()),
                fps=float(self.fps_spin.value()),
                gain=float(self.gain_spin.value()),
                resolution=(sel_w, sel_h),
            )
            self.worker.set_preview_target_fps(float(self.fps_spin.value()))
            self.log(f"Selected output resolution: {sel_w}x{sel_h} (whole-frame downsample)")
        except Exception as exc:
            self.log(f"ERROR: Apply settings failed: {exc}")

    def on_roi_select_toggled(self, enabled: bool):
        self.preview_label.set_roi_select_enabled(bool(enabled))
        if enabled:
            self.log("ROI selection mode enabled: draw one rectangle on preview")
        else:
            self.preview_label.set_roi_overlay_rect(None)

    def _stop_roi_select_mode(self):
        self.roi_select_btn.blockSignals(True)
        self.roi_select_btn.setChecked(False)
        self.roi_select_btn.blockSignals(False)
        self.preview_label.set_roi_select_enabled(False)
        self.preview_label.set_roi_overlay_rect(None)

    def clear_roi(self, log_message: bool = True):
        self._roi_frame_rect = None
        self._stop_roi_select_mode()
        self.preview_label.set_roi_overlay_rect(None)
        try:
            self.worker.set_roi_rect(None)
        except Exception as exc:
            self.log(f"ERROR: Clear ROI failed: {exc}")
            return
        if log_message:
            self.log("ROI cleared")

    def _display_rect_to_frame_rect(self, display_rect: QRect) -> Optional[Tuple[int, int, int, int]]:
        if self._display_map is None:
            return None
        x0, y0, disp_w, disp_h, frame_w, frame_h = self._display_map
        disp_bounds = QRect(x0, y0, disp_w, disp_h)
        rect = display_rect.intersected(disp_bounds)
        if rect.width() < 2 or rect.height() < 2:
            return None

        x1 = max(0, min(frame_w - 1, int((rect.left() - x0) * frame_w / max(1, disp_w))))
        y1 = max(0, min(frame_h - 1, int((rect.top() - y0) * frame_h / max(1, disp_h))))
        x2 = max(0, min(frame_w, int((rect.right() - x0 + 1) * frame_w / max(1, disp_w))))
        y2 = max(0, min(frame_h, int((rect.bottom() - y0 + 1) * frame_h / max(1, disp_h))))
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        if w < 2 or h < 2:
            return None
        return (x1, y1, w, h)

    def _frame_rect_to_display_rect(self, frame_rect: Tuple[int, int, int, int]) -> Optional[QRect]:
        if self._display_map is None:
            return None
        x0, y0, disp_w, disp_h, frame_w, frame_h = self._display_map
        fx, fy, fw, fh = frame_rect
        fx = max(0, min(fx, frame_w - 1))
        fy = max(0, min(fy, frame_h - 1))
        fw = max(1, min(fw, frame_w - fx))
        fh = max(1, min(fh, frame_h - fy))
        if frame_w <= 0 or frame_h <= 0:
            return None
        left = int(x0 + fx * disp_w / frame_w)
        top = int(y0 + fy * disp_h / frame_h)
        right = int(x0 + (fx + fw) * disp_w / frame_w) - 1
        bottom = int(y0 + (fy + fh) * disp_h / frame_h) - 1
        return QRect(QPoint(left, top), QPoint(max(left, right), max(top, bottom))).normalized()

    def on_preview_roi_drawn(self, display_rect: QRect):
        if not self.preview_label.is_roi_select_enabled():
            return
        frame_rect = self._display_rect_to_frame_rect(display_rect)
        if frame_rect is None:
            self.log("ROI ignored: selection is outside current frame area")
            self._stop_roi_select_mode()
            return
        self._roi_frame_rect = frame_rect
        try:
            self.worker.set_roi_rect(frame_rect)
        except Exception as exc:
            self.log(f"ERROR: Set ROI failed: {exc}")
            self._stop_roi_select_mode()
            return
        x, y, w, h = frame_rect
        self.log(f"ROI selected: x={x}, y={y}, w={w}, h={h}")
        self._stop_roi_select_mode()

    def apply_sync_settings(self):
        if not self._connected:
            return
        try:
            self.worker.apply_sync_output(
                enabled=self.sync_enable_check.isChecked(),
                source_mode=self.sync_mode_combo.currentText().strip(),
                pulse_duration_us=float(self.sync_pulse_us_spin.value()),
                output_line=self.sync_output_line_combo.currentText().strip(),
            )
        except Exception as exc:
            self.log(f"ERROR: Apply sync settings failed: {exc}")

    def apply_buffer_settings(self):
        if not self._connected:
            return
        strategy_idx = self.grab_strategy_combo.currentIndex()
        try:
            self.worker.apply_buffer_settings(
                image_node_num=int(self.image_node_spin.value()),
                grab_strategy=int(strategy_idx),
                output_queue_size=int(self.output_queue_spin.value()),
            )
        except Exception as exc:
            self.log(f"ERROR: Apply buffer settings failed: {exc}")

    def _apply_exposure_live(self, value: float):
        if not self._connected:
            return
        try:
            self.worker.set_exposure_only(float(value))
        except Exception as exc:
            self.log(f"ERROR: Exposure live update failed: {exc}")

    def _apply_frame_rate_live(self, value: float):
        if not self._connected:
            return
        try:
            self.worker.set_frame_rate_only(float(value))
            self.worker.set_preview_target_fps(float(value))
        except Exception as exc:
            self.log(f"ERROR: Frame-rate live update failed: {exc}")

    def _apply_gain_live(self, value: float):
        if not self._connected:
            return
        try:
            self.worker.set_gain_only(float(value))
        except Exception as exc:
            self.log(f"ERROR: Gain live update failed: {exc}")

    def _apply_resolution_live(self):
        if not self._connected:
            return
        try:
            sel_w, sel_h = self._selected_resolution()
            self.worker.set_resolution_only(int(sel_w), int(sel_h))
        except Exception as exc:
            self.log(f"ERROR: Resolution live update failed: {exc}")

    def start_live(self):
        if not self._connected:
            self.log("Connect camera first")
            return
        # Live should run in free-run mode unless acquisition explicitly arms trigger mode.
        self.apply_trigger_settings(enable_override=False)
        self.apply_sync_settings()
        self.worker.start_live()
        self._set_live_controls(True)
        self.log("Live mode started")

    def stop_live(self):
        self.worker.stop_live()
        self._set_live_controls(False)
        self.log("Live mode stopped")

    def on_snapshot(self):
        if not self._connected:
            self.log("Connect camera first")
            return
        folder = self.save_folder_edit.text().strip() or os.getcwd()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{self.base_name_edit.text().strip()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
        self.worker.snapshot(path)

    def set_module_save_config(self, folder: str, base_name: str) -> None:
        """Called by the master GUI to set save location in module mode."""
        self._module_save_folder = folder
        self._module_base_name = base_name
        self.save_folder_edit.setText(folder)
        self.base_name_edit.setText(base_name)

    def set_module_auto_start(self, enabled: bool) -> None:
        """Called by the master GUI to toggle auto-start in module mode."""
        self.auto_start_check.blockSignals(True)
        self.auto_start_check.setChecked(enabled)
        self.auto_start_check.blockSignals(False)
        self.on_auto_start_toggled(enabled)

    def set_module_start_recording(self) -> None:
        """Called by master to directly start recording (no trigger gating)."""
        if not self._connected:
            return
        self.apply_sync_settings()
        self.worker.arm_recording(
            save_cfg=self._build_save_cfg(),
            trigger_enabled=False,
            stop_by_trigger=False,
            auto_start=False,
        )
        self.worker.start_live()
        self._set_acq_controls(True)
        self._set_live_controls(True)
        self.log("Recording started (master command)")

    def set_module_stop_recording(self) -> None:
        """Called by master to stop recording."""
        if not self._connected:
            return
        self.worker.stop_recording(send_end_pulse=False)
        self._set_acq_controls(False)
        self.log("Recording stopped (master command)")

    def set_module_sync_enabled(self, enabled: bool) -> None:
        """Called by the master GUI to toggle sync/strobe output in module mode."""
        self.sync_enable_check.blockSignals(True)
        self.sync_enable_check.setChecked(bool(enabled))
        self.sync_enable_check.blockSignals(False)
        self.apply_sync_settings()

    def set_module_sync_profile(self, output_line: str, source_mode: str, pulse_us: float) -> None:
        """Called by the master GUI to push sync profile settings in module mode."""
        line_text = (output_line or "Line1").strip() or "Line1"
        mode_text = (source_mode or "Frame start active").strip() or "Frame start active"
        pulse_val = float(max(1.0, pulse_us))

        # Block signals to avoid duplicate apply_sync_settings calls.
        for w in (self.sync_output_line_combo, self.sync_mode_combo, self.sync_pulse_us_spin):
            w.blockSignals(True)

        idx = self.sync_output_line_combo.findText(line_text)
        if idx >= 0:
            self.sync_output_line_combo.setCurrentIndex(idx)

        mode_idx = self.sync_mode_combo.findText(mode_text)
        if mode_idx >= 0:
            self.sync_mode_combo.setCurrentIndex(mode_idx)

        self.sync_pulse_us_spin.setValue(pulse_val)

        for w in (self.sync_output_line_combo, self.sync_mode_combo, self.sync_pulse_us_spin):
            w.blockSignals(False)

        self.apply_sync_settings()

    def _build_save_cfg(self) -> SaveConfig:
        if self._module_mode:
            folder = self._module_save_folder or os.getcwd()
            base = self._module_base_name or "camera_run"
        else:
            folder = self.save_folder_edit.text().strip() or os.getcwd()
            base = self.base_name_edit.text().strip() or "camera_run"
        selected_ext, selected_fourcc_candidates = self.codec_profile_combo.currentData()

        return SaveConfig(
            folder=folder,
            base_name=base,
            file_ext=selected_ext,
            fourcc_candidates=list(selected_fourcc_candidates),
            fps=float(self.fps_spin.value()),
            queue_size=int(self.queue_size_spin.value()),
            drop_if_full=self.drop_frames_check.isChecked(),
        )

    def start_acquisition(self):
        if not self._connected:
            self.log("Connect camera first")
            return

        if self.auto_start_check.isChecked():
            self.log("Auto-start is enabled; acquisition will be controlled by trigger level automatically")
            self.worker.start_live()
            self._set_live_controls(True)
            self._set_acq_controls(True)
            return

        self.apply_trigger_settings(enable_override=self.trigger_enable_check.isChecked())
        self.apply_sync_settings()
        self.worker.arm_recording(
            save_cfg=self._build_save_cfg(),
            trigger_enabled=self.trigger_enable_check.isChecked(),
            stop_by_trigger=True,
            auto_start=False,
        )
        self.worker.start_live()
        self._set_acq_controls(True)
        self._set_live_controls(True)
        if self.trigger_enable_check.isChecked():
            self.log("Acquisition armed, waiting for trigger HIGH (will stop on LOW)")
        else:
            self.log("Acquisition running")

    def stop_acquisition(self):
        if self.auto_start_check.isChecked():
            self.on_auto_start_toggled(False)
            self.auto_start_check.blockSignals(True)
            self.auto_start_check.setChecked(False)
            self.auto_start_check.blockSignals(False)
        self.worker.stop_recording(send_end_pulse=True)
        self.apply_trigger_settings(enable_override=False)
        self._set_acq_controls(False)
        self._set_live_controls(False)
        self.log("Acquisition stopped")

    def on_browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder", self.save_folder_edit.text().strip() or os.getcwd())
        if folder:
            self.save_folder_edit.setText(folder)

    def on_connected_state(self, is_connected: bool):
        self._connected = is_connected
        self.snapshot_btn.setEnabled(is_connected)
        self._set_live_controls(False)
        self._set_acq_controls(False)
        if not is_connected:
            self.fps_label.setText("Camera FPS: --")
            self._is_recording_now = False
            self._record_flash_timer.stop()
            self._set_record_indicator_idle()

    def on_limits_ready(self, payload: dict):
        gain_range = payload.get("gain_range")
        if gain_range is not None:
            g_min, g_max, g_cur = gain_range
            self.gain_spin.setRange(float(g_min), float(g_max))
            self.gain_spin.setValue(float(g_cur))
            self.gain_range_label.setText(f"Max: {g_max:.2f} dB")
            self.log(f"Gain range: {g_min:.3f} to {g_max:.3f}")

        res_range = payload.get("res_range")
        if res_range is not None:
            w_min, w_max, w_inc, h_min, h_max, h_inc = res_range
            self.log(f"Width range: {w_min}-{w_max} (inc {w_inc}), Height range: {h_min}-{h_max} (inc {h_inc})")

    def on_record_state(self, is_recording: bool):
        self._is_recording_now = bool(is_recording)
        if self._is_recording_now:
            self._record_indicator_on = False
            self._record_flash_timer.start()
            self._toggle_record_indicator()
        else:
            self._record_flash_timer.stop()
            self._set_record_indicator_idle()

        if not is_recording:
            if not self.auto_start_check.isChecked():
                self._set_acq_controls(False)

    def on_camera_fps(self, fps_value: float):
        self.fps_label.setText(f"Camera FPS: {fps_value:.1f}")

    def on_buffer_stats(self, stats: dict):
        sw_used = int(stats.get("sw_used", 0))
        sw_max = int(stats.get("sw_max", 0))
        drops = int(stats.get("drops", 0))
        self.sw_buf_label.setText(f"Buffer: {sw_used}/{sw_max} drop:{drops}")
        fill = (float(sw_used) / float(sw_max)) if sw_max > 0 else 0.0
        if fill >= 0.9:
            self.sw_buf_label.setStyleSheet("color:#ff5a5a;")
        elif fill >= 0.75:
            self.sw_buf_label.setStyleSheet("color:#f5b342;")
        else:
            self.sw_buf_label.setStyleSheet("")

    def on_frame(self, frame: np.ndarray):
        if frame.ndim == 2:
            h, w = frame.shape
            qimg = QImage(frame.data, w, h, w, QImage.Format.Format_Grayscale8)
        else:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(qimg)

        # Do not upscale to avoid auto-zoom/stretch in live mode.
        target_w = max(1, self.preview_label.width() - 8)
        target_h = max(1, self.preview_label.height() - 8)
        if w > target_w or h > target_h:
            pix = pix.scaled(
                target_w,
                target_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        disp_w = pix.width()
        disp_h = pix.height()
        x0 = max(0, (self.preview_label.width() - disp_w) // 2)
        y0 = max(0, (self.preview_label.height() - disp_h) // 2)
        self._display_map = (x0, y0, max(1, disp_w), max(1, disp_h), int(w), int(h))

        self.preview_label.set_roi_overlay_rect(None)

        self.preview_label.setPixmap(pix)

    def closeEvent(self, event):
        try:
            self.worker.shutdown()
            self.worker.disconnect_camera()
            if isinstance(self._backend_obj, HikMVSCameraBackend):
                self._backend_obj.finalize()
        except Exception:
            pass
        self.worker_thread.quit()
        self.worker_thread.wait(1500)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = CameraGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
