"""BehaviorHub GUI — Master experiment controller.

Replicates the NI_V1.4_camera.py session logic with a PyQt6 UI,
coordinates camera recording via a separate process, and provides
velocity/location visualization.

to install as exe: pyinstaller --noconfirm BehaviorHub_easy.spec
"""

import csv
import json
import math
import multiprocessing
import os
import random
import sys
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import nidaqmx
from nidaqmx.constants import (
    AcquisitionType,
    AngleUnits,
    CountDirection,
    Edge,
    EncoderType,
    LineGrouping,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def parse_signed_32bit(number: int) -> int:
    number = int(number)
    if number >= (1 << 31):
        number -= (1 << 32)
    return number


def _edge_from_config(edge_name: str) -> Edge:
    return Edge.FALLING if str(edge_name).strip().lower() == "falling" else Edge.RISING


def _normalize_counter_terminal(pin: str, device_name: str) -> str:
    """Map IO-table pin strings to a counter-compatible PFI terminal."""
    pin_text = str(pin or "").strip()
    if not pin_text:
        return ""

    lowered = pin_text.lower().replace(" ", "")
    if "/pfi" in lowered:
        # Already PFI-style, preserve user-specified device alias casing.
        return pin_text

    parts = lowered.split("/")
    if len(parts) >= 3 and parts[-2] == "port0" and parts[-1].startswith("line"):
        line_suffix = parts[-1][4:]
        if line_suffix.isdigit():
            return f"/{device_name}/PFI{int(line_suffix)}"
    return ""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class IOLine:
    pin: str
    direction: str          # "Input" or "Output"
    function: str           # Human-readable, used as CSV column name
    input_kind: str = "digital"  # "digital" or "analog"


@dataclass
class EventDef:
    """One scheduled behavioural event fired by the NI card."""
    time_ms: float          # Delay from session start (ms)
    pin: str                # NI DO line, e.g. "Dev2/port0/line6"
    pulse_width_ms: float   # Pulse HIGH duration (ms)
    jitter_enabled: bool = False  # Apply randomized onset jitter at session start
    remark: str = ""        # Human-readable label


@dataclass
class SessionConfig:
    """Everything the NI worker needs to run one recording session."""
    save_path: str

    # NI device
    device_name: str = "Dev1"

    # Pin assignments (defaults from NI_V1.4_camera.py)
    frame_clock_pin: str = "/Dev1/PFI0"
    microscope_start_line: str = "Dev1/port0/line2"
    arduino_input_line: str = "Dev1/port0/line3"
    encoder_a_pfi: str = "/Dev1/PFI4"
    encoder_b_pfi: str = "/Dev1/PFI5"
    camera_sync_line: str = "Dev1/port0/line14"
    camera_sync_counter_pin: str = "/Dev1/PFI14"
    camera_trigger_pin: str = "Dev1/port0/line15"

    # Arduino command pins (NI → Arduino, hardcoded)
    buzz_cmd_pin: str = "Dev1/port0/line6"   # PFI6 → Arduino D12
    puff_cmd_pin: str = "Dev1/port0/line7"   # PFI7 → Arduino D11

    # Counters
    time_counter: str = "Dev1/ctr0"
    camera_sync_counter: str = "Dev1/ctr1"
    encoder_counter: str = "Dev1/ctr2"

    # Physical
    internal_timebase: str = "/Dev1/20MHzTimebase"
    timebase_freq: float = 20_000_000.0
    estimated_fps: float = 7000.0

    # Encoder
    wheel_diameter_cm: float = 15.5
    encoder_ppr: int = 1024
    smoothing_window_s: float = 0.05

    # Session
    silence_timeout_s: float = 0.3
    microscope_pulse_s: float = 0.100
    camera_sync_active_edge: str = "falling"

    # Feature flags
    enable_camera_sync: bool = True
    enable_microscope_sync: bool = True

    # Dynamic IO from UI table
    input_channels: List[IOLine] = field(default_factory=list)

    # Scheduled behavioural events
    events: List[EventDef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# NI Session Worker  (runs on QThread)
# ---------------------------------------------------------------------------

class NISessionWorker(QObject):
    """Replicates NI_V1.4_camera.py session logic exactly."""
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    velocity = pyqtSignal(float)
    frame_count = pyqtSignal(int)
    camera_frame_count = pyqtSignal(int)
    finished = pyqtSignal(str)

    def __init__(self, config: SessionConfig, shared_cam_counter=None):
        super().__init__()
        self.config = config
        self._stop_event = threading.Event()
        self._shared_cam_counter = shared_cam_counter

    def request_stop(self) -> None:
        self._stop_event.set()

    # ---- CSV header from IO-table function names ----
    def _build_csv_header(self) -> list:
        header = ["Frame_ID", "Time_s"]
        for ch in self.config.input_channels:
            header.append(ch.function)
        header.extend(["Raw_Ticks_Signed", "Zeroed_Dist_cm", "Smoothed_Vel_cm_s"])
        return header

    def run(self) -> None:
        try:
            self._run_impl()
        except Exception as exc:
            self.error.emit(str(exc))

    def _run_impl(self) -> None:
        cfg = self.config
        cm_per_rev = cfg.wheel_diameter_cm * math.pi
        ticks_per_rev = cfg.encoder_ppr * 4
        cm_per_tick = cm_per_rev / ticks_per_rev

        # Gather user-selected inputs and split by signal kind.
        all_channels = [ch for ch in cfg.input_channels if ch.direction == "Input"]
        if not all_channels:
            raise RuntimeError("No input channels configured in IO table.")
        di_channels = [ch for ch in all_channels if (ch.input_kind or "digital") == "digital"]
        ai_channels = [ch for ch in all_channels if (ch.input_kind or "digital") == "analog"]
        di_csv = ",".join(ch.pin for ch in di_channels)

        dynamic_header = self._build_csv_header()

        self.status.emit("Creating NI tasks...")

        total_frames = 0
        camera_frame_count = 0
        last_data_time = time.time()
        session_wall_start = time.time()
        start_ticks_time = None
        start_ticks_enc = None
        history_buffer: deque = deque()
        flush_deadline = time.time() + 1.0
        stopped_by = "Abort"
        arduino_onsets: list = []  # list of onset times (s)
        last_exact_time_s: float = 0.0  # NI-timebase duration (ground truth)

        use_counter_sync = cfg.enable_camera_sync

        with nidaqmx.Task() as logger_data, \
             nidaqmx.Task() as logger_ai, \
             nidaqmx.Task() as logger_time, \
             nidaqmx.Task() as logger_enc, \
             nidaqmx.Task() as logger_camcnt, \
             nidaqmx.Task() as camera_trigger_task:

            # 1. DATA LOGGER (DI) — frame-clock sampled digital lines
            if di_channels:
                logger_data.di_channels.add_di_chan(
                    di_csv, line_grouping=LineGrouping.CHAN_PER_LINE
                )
                logger_data.timing.cfg_samp_clk_timing(
                    rate=cfg.estimated_fps,
                    source=cfg.frame_clock_pin,
                    active_edge=Edge.FALLING,
                    sample_mode=AcquisitionType.CONTINUOUS,
                )

            # 1b. DATA LOGGER (AI) — frame-clock sampled analog lines
            if ai_channels:
                for ch in ai_channels:
                    logger_ai.ai_channels.add_ai_voltage_chan(ch.pin)
                logger_ai.timing.cfg_samp_clk_timing(
                    rate=cfg.estimated_fps,
                    source=cfg.frame_clock_pin,
                    active_edge=Edge.FALLING,
                    sample_mode=AcquisitionType.CONTINUOUS,
                )

            # 2. TIME LOGGER — 20 MHz counter for precise timestamps
            ctr_time = logger_time.ci_channels.add_ci_count_edges_chan(
                counter=cfg.time_counter,
                edge=Edge.RISING,
                initial_count=0,
                count_direction=CountDirection.COUNT_UP,
            )
            ctr_time.ci_count_edges_term = cfg.internal_timebase
            logger_time.timing.cfg_samp_clk_timing(
                rate=cfg.estimated_fps,
                source=cfg.frame_clock_pin,
                active_edge=Edge.FALLING,
                sample_mode=AcquisitionType.CONTINUOUS,
            )

            # 3. ENCODER LOGGER — X4 quadrature on PFI4/PFI5
            encoder_channel = logger_enc.ci_channels.add_ci_ang_encoder_chan(
                counter=cfg.encoder_counter,
                decoding_type=EncoderType.X_4,
                units=AngleUnits.TICKS,
                pulses_per_rev=cfg.encoder_ppr,
                initial_angle=0.0,
            )
            encoder_channel.ci_encoder_a_input_term = cfg.encoder_a_pfi
            encoder_channel.ci_encoder_b_input_term = cfg.encoder_b_pfi
            logger_enc.timing.cfg_samp_clk_timing(
                rate=cfg.estimated_fps,
                source=cfg.frame_clock_pin,
                active_edge=Edge.FALLING,
                sample_mode=AcquisitionType.CONTINUOUS,
            )

            # 4. CAMERA SYNC COUNTER (optional)
            if use_counter_sync:
                if not cfg.camera_sync_counter_pin:
                    use_counter_sync = False
                    self.status.emit(
                        "Camera sync counter pin could not be mapped to PFI; falling back to sampled DI."
                    )
                else:
                    try:
                        cam_counter = logger_camcnt.ci_channels.add_ci_count_edges_chan(
                            counter=cfg.camera_sync_counter,
                            edge=_edge_from_config(cfg.camera_sync_active_edge),
                            initial_count=0,
                            count_direction=CountDirection.COUNT_UP,
                        )
                        cam_counter.ci_count_edges_term = cfg.camera_sync_counter_pin
                        logger_camcnt.timing.cfg_samp_clk_timing(
                            rate=cfg.estimated_fps,
                            source=cfg.frame_clock_pin,
                            active_edge=Edge.FALLING,
                            sample_mode=AcquisitionType.CONTINUOUS,
                        )
                    except Exception as exc:
                        use_counter_sync = False
                        self.status.emit(
                            f"Camera sync counter setup failed ({exc}); falling back to sampled DI."
                        )

            # 5. CAMERA TRIGGER GATE — level output on camera trigger pin
            camera_trigger_task.do_channels.add_do_chan(cfg.camera_trigger_pin)
            camera_trigger_task.write(False)

            # Prepare CSV
            save_folder = os.path.dirname(cfg.save_path)
            if save_folder:
                os.makedirs(save_folder, exist_ok=True)

            with open(cfg.save_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(dynamic_header)

                self.status.emit("Arming system...")

                # Start all input tasks
                logger_enc.start()
                logger_time.start()
                if di_channels:
                    logger_data.start()
                if ai_channels:
                    logger_ai.start()
                if use_counter_sync:
                    logger_camcnt.start()

                # Optional microscope start pulse; camera trigger gate is controlled separately.
                if cfg.enable_microscope_sync:
                    self.status.emit(
                        "Sending microscope start pulse and asserting camera trigger HIGH..."
                    )
                    with nidaqmx.Task() as t_scope:
                        t_scope.do_channels.add_do_chan(cfg.microscope_start_line)
                        t_scope.write(False)
                        t_scope.write(True)
                        if cfg.enable_camera_sync:
                            camera_trigger_task.write(True)
                        time.sleep(max(0.001, cfg.microscope_pulse_s))
                        t_scope.write(False)
                else:
                    if cfg.enable_camera_sync:
                        camera_trigger_task.write(True)
                    self.status.emit(
                        "Microscope sync disabled; skipped microscope start pulse."
                    )

                self.status.emit("Recording... (waiting for frames)")

                # ── Event scheduler (background thread) ──────────────
                # Fires timed DO pulses on dedicated NI tasks.
                # Runs in a daemon thread so it cannot block the main
                # recording loop (which reads at microscope speed).
                # Each event opens its own nidaqmx.Task briefly; the
                # recording-loop tasks are on different DI channels /
                # counters, so there is zero resource contention.
                #
                # IMPORTANT: The scheduler waits for the first
                # microscope frame before starting its epoch so that
                # event times align with the CSV timestamps.
                evt_stop = self._stop_event          # share the same stop flag
                first_frame_arrived = threading.Event()
                evt_t0_holder: list = [0.0]          # set by recording loop
                sorted_events = sorted(cfg.events, key=lambda e: e.time_ms)

                def _event_scheduler() -> None:
                    """Background thread: fire scheduled DO pulses."""
                    # Block until the first microscope frame arrives
                    if not first_frame_arrived.wait(timeout=30):
                        return  # timed out — no frames ever came
                    evt_t0 = evt_t0_holder[0]
                    for ev in sorted_events:
                        target_s = ev.time_ms / 1000.0
                        # Spin-sleep until target time or abort
                        while True:
                            if evt_stop.is_set():
                                return
                            remaining = target_s - (time.perf_counter() - evt_t0)
                            if remaining <= 0:
                                break
                            time.sleep(min(remaining, 0.050))
                        # Fire the pulse
                        try:
                            with nidaqmx.Task() as t_evt:
                                t_evt.do_channels.add_do_chan(ev.pin)
                                t_evt.write(True)
                                time.sleep(max(0.001, ev.pulse_width_ms / 1000.0))
                                t_evt.write(False)
                        except Exception:
                            pass  # best-effort; NI errors logged elsewhere

                if sorted_events:
                    evt_thread = threading.Thread(
                        target=_event_scheduler, daemon=True, name="EventScheduler"
                    )
                    evt_thread.start()
                    self.status.emit(
                        f"Event scheduler started ({len(sorted_events)} event(s))"
                    )
                else:
                    evt_thread = None

                prev_arduino_state = None
                prev_camera_sync_state = None
                camera_sync_count_from_di = 0
                cam_counter_start = None
                arduino_ch_idx = None
                cam_sync_idx = None
                for idx_ch, ch in enumerate(all_channels):
                    if ch.function == "Arduino_State":
                        arduino_ch_idx = idx_ch
                    elif ch.function == "Camera_Frame":
                        cam_sync_idx = idx_ch

                try:
                    while not self._stop_event.is_set():
                        samples_available = (
                            logger_data.in_stream.avail_samp_per_chan
                            if di_channels
                            else logger_time.in_stream.avail_samp_per_chan
                        )

                        if samples_available == 0:
                            if (
                                (time.time() - last_data_time)
                                > cfg.silence_timeout_s
                                and total_frames > 0
                            ):
                                stopped_by = "Silence detected (microscope stopped)"
                                break
                            time.sleep(0.005)
                            continue

                        if di_channels:
                            chunk_data = logger_data.read(
                                number_of_samples_per_channel=samples_available
                            )
                        else:
                            chunk_data = []
                        if ai_channels:
                            chunk_ai = logger_ai.read(
                                number_of_samples_per_channel=samples_available
                            )
                        else:
                            chunk_ai = []
                        if use_counter_sync:
                            chunk_camcnt = logger_camcnt.read(
                                number_of_samples_per_channel=samples_available
                            )
                        else:
                            chunk_camcnt = []
                        chunk_time = logger_time.read(
                            number_of_samples_per_channel=samples_available
                        )
                        chunk_enc = logger_enc.read(
                            number_of_samples_per_channel=samples_available
                        )
                        last_data_time = time.time()

                        # Normalize multi-channel data
                        if not isinstance(chunk_data, list):
                            chunk_data = [chunk_data]
                        if not isinstance(chunk_time, list):
                            chunk_time = [chunk_time]
                        if not isinstance(chunk_enc, list):
                            chunk_enc = [chunk_enc]
                        if use_counter_sync and not isinstance(chunk_camcnt, list):
                            chunk_camcnt = [chunk_camcnt]

                        if di_channels:
                            if len(di_channels) == 1:
                                di_by_channel = [chunk_data]
                            elif (
                                len(chunk_data) >= 2
                                and isinstance(chunk_data[0], list)
                            ):
                                di_by_channel = chunk_data
                            else:
                                di_by_channel = [chunk_data]
                        else:
                            di_by_channel = []

                        if ai_channels:
                            if len(ai_channels) == 1:
                                ai_by_channel = [chunk_ai]
                            elif (
                                len(chunk_ai) >= 2
                                and isinstance(chunk_ai[0], list)
                            ):
                                ai_by_channel = chunk_ai
                            else:
                                ai_by_channel = [chunk_ai]
                        else:
                            ai_by_channel = []

                        length_candidates = [len(chunk_time), len(chunk_enc)]
                        if di_by_channel:
                            length_candidates.extend(len(ch_data) for ch_data in di_by_channel)
                        if ai_by_channel:
                            length_candidates.extend(len(ch_data) for ch_data in ai_by_channel)
                        if use_counter_sync:
                            length_candidates.append(len(chunk_camcnt))
                        sample_count = min(length_candidates)

                        rows = []
                        for i in range(sample_count):
                            curr_ticks_time = chunk_time[i]

                            if start_ticks_time is None:
                                start_ticks_time = curr_ticks_time
                                start_ticks_enc = parse_signed_32bit(chunk_enc[i])
                                # Sync event scheduler epoch to first frame
                                evt_t0_holder[0] = time.perf_counter()
                                first_frame_arrived.set()

                            exact_time_s = (
                                (curr_ticks_time - start_ticks_time)
                                / cfg.timebase_freq
                            )
                            total_frames += 1
                            last_exact_time_s = exact_time_s

                            # Input values in the configured channel order.
                            input_values = []
                            di_idx = 0
                            ai_idx = 0
                            for ch in all_channels:
                                if (ch.input_kind or "digital") == "analog":
                                    val = (
                                        float(ai_by_channel[ai_idx][i])
                                        if ai_idx < len(ai_by_channel)
                                        else float("nan")
                                    )
                                    ai_idx += 1
                                    input_values.append(val)
                                else:
                                    val = (
                                        int(bool(di_by_channel[di_idx][i]))
                                        if di_idx < len(di_by_channel)
                                        else 0
                                    )
                                    di_idx += 1
                                    input_values.append(val)

                            # Detect Arduino event onsets (rising edge)
                            if arduino_ch_idx is not None:
                                ard_raw = input_values[arduino_ch_idx]
                                ard_val = 1 if float(ard_raw) >= 0.5 else 0
                                if prev_arduino_state is not None and prev_arduino_state == 0 and ard_val == 1:
                                    arduino_onsets.append(exact_time_s)
                                prev_arduino_state = ard_val

                            # NI-sampled camera sync edge count fallback (frame-clock aligned).
                            if cam_sync_idx is not None:
                                cam_raw = input_values[cam_sync_idx]
                                cam_val = 1 if float(cam_raw) >= 0.5 else 0
                                if prev_camera_sync_state is None:
                                    # If session starts while sync is already HIGH,
                                    # count that first observed high as one frame.
                                    if cam_val == 1:
                                        camera_sync_count_from_di += 1
                                    prev_camera_sync_state = cam_val
                                elif prev_camera_sync_state == 0 and cam_val == 1:
                                    camera_sync_count_from_di += 1
                                prev_camera_sync_state = cam_val

                            # Camera frame count for CSV: prefer NI-sampled Camera_Frame DI
                            # rising-edge count (stable and microscope-clock aligned).
                            if cam_sync_idx is not None:
                                camera_frame_count = camera_sync_count_from_di
                            elif use_counter_sync and i < len(chunk_camcnt):
                                raw_cam_count = int(chunk_camcnt[i])
                                if cam_counter_start is None:
                                    cam_counter_start = raw_cam_count
                                camera_frame_count = max(0, raw_cam_count - cam_counter_start)

                            # Encoder
                            curr_ticks_enc_signed = parse_signed_32bit(
                                chunk_enc[i]
                            )
                            current_dist_cm = (
                                (curr_ticks_enc_signed - start_ticks_enc)
                                * cm_per_tick
                            )

                            # Sliding-window velocity
                            history_buffer.append((exact_time_s, current_dist_cm))
                            while (
                                len(history_buffer) > 1
                                and (
                                    exact_time_s - history_buffer[0][0]
                                )
                                > cfg.smoothing_window_s
                            ):
                                history_buffer.popleft()
                            old_time, old_dist = history_buffer[0]
                            delta_t = exact_time_s - old_time
                            delta_d = current_dist_cm - old_dist
                            smoothed_velocity = (
                                (delta_d / delta_t) if delta_t > 0 else 0.0
                            )

                            # Replace raw Camera_Frame DI (0/1) with cumulative count
                            if cam_sync_idx is not None:
                                input_values[cam_sync_idx] = camera_frame_count

                            rows.append([
                                total_frames,
                                f"{exact_time_s:.6f}",
                                *input_values,
                                curr_ticks_enc_signed,
                                f"{current_dist_cm:.4f}",
                                f"{smoothed_velocity:.4f}",
                            ])

                        writer.writerows(rows)

                        if rows:
                            self.velocity.emit(float(rows[-1][-1]))
                            self.frame_count.emit(total_frames)
                            self.camera_frame_count.emit(camera_frame_count)

                        now = time.time()
                        if now >= flush_deadline:
                            f.flush()
                            flush_deadline = now + 1.0

                        time.sleep(0.01)

                except KeyboardInterrupt:
                    stopped_by = "Keyboard interrupt"

                # Wait for event scheduler to finish / abort
                if evt_thread is not None:
                    evt_stop.set()  # signal (may already be set)
                    evt_thread.join(timeout=2.0)

                f.flush()

            # Deassert camera trigger
            self.status.emit("Deasserting camera trigger (LOW)...")
            camera_trigger_task.write(False)

        if self._stop_event.is_set() and stopped_by == "Abort":
            stopped_by = "Aborted by user"

        wall_duration_s = max(0.0, time.time() - session_wall_start)

        # Use NI-timebase duration (from CSV) as ground truth
        if total_frames > 0 and start_ticks_time is not None:
            scope_fps = total_frames / last_exact_time_s if last_exact_time_s > 0 else 0.0
        else:
            scope_fps = 0.0

        duration_gap_ms = max(0.0, (wall_duration_s - last_exact_time_s) * 1000.0)

        summary_parts = [
            f"Done. Scope frames: {total_frames}",
            f"Camera frames: {camera_frame_count}",
            f"Duration(scope): {last_exact_time_s:.2f}s",
            f"Duration(wall): {wall_duration_s:.2f}s",
            f"Scope-wall gap: {duration_gap_ms:.1f}ms",
            f"Microscope fps: {scope_fps:.1f}",
        ]
        if arduino_onsets:
            onset_strs = ", ".join(f"{t:.3f}" for t in arduino_onsets)
            summary_parts.append(f"Arduino onsets(s): [{onset_strs}]")
        else:
            summary_parts.append("Arduino onsets: none")
        summary_parts.append(f"Stop: {stopped_by}")
        summary_parts.append(f"File: {cfg.save_path}")

        self.finished.emit(". ".join(summary_parts))


# ---------------------------------------------------------------------------
# Camera process launcher
# ---------------------------------------------------------------------------

def _camera_process_main(
    save_folder: str,
    base_name: str,
    cmd_queue: multiprocessing.Queue,
    log_queue: multiprocessing.Queue,
    shared_frame_counter=None,
) -> None:
    """Entry point for the camera module subprocess.

    Launches the camera GUI as a standalone window in a fresh Python
    process.  The *cmd_queue* carries commands from the master GUI
    (e.g. ``("set_save", folder, name)``, ``("auto_start", True)``,
    ``("quit",)``).  The *log_queue* is used to send log messages back
    to the master GUI.
    """
    # Import here so the MVS SDK is loaded only inside the child process.
    from PyQt6.QtWidgets import QApplication as _QApp
    from PyQt6.QtCore import QTimer as _QTimer

    # camera_GUI lives next to this file
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from camera_GUI import CameraGUI

    app = _QApp(sys.argv)
    gui = CameraGUI(module_mode=True, hub_mode=True)
    gui.set_module_save_config(save_folder, base_name)
    if shared_frame_counter is not None:
        gui.set_shared_frame_counter(shared_frame_counter)
    gui.show()

    # Poll the command queue periodically
    def _poll_commands() -> None:
        while not cmd_queue.empty():
            try:
                msg = cmd_queue.get_nowait()
            except Exception:
                break
            if not msg:
                continue
            cmd = msg[0]
            if cmd == "set_save":
                gui.set_module_save_config(msg[1], msg[2])
            elif cmd == "auto_start":
                gui.set_module_auto_start(bool(msg[1]))
            elif cmd == "sync_enable":
                gui.set_module_sync_enabled(bool(msg[1]))
            elif cmd == "sync_profile":
                gui.set_module_sync_profile(str(msg[1]), str(msg[2]), float(msg[3]))
            elif cmd == "camera_profile":
                gui.set_module_camera_profile(
                    exposure_us=float(msg[1]),
                    gain_db=float(msg[2]),
                    fps=float(msg[3]),
                    resolution=str(msg[4]),
                )
            elif cmd == "recording_profile":
                gui.set_module_recording_profile(
                    codec_index=int(msg[1]),
                    queue_size=int(msg[2]),
                    drop_if_full=bool(msg[3]),
                )
            elif cmd == "trigger_profile":
                gui.set_module_trigger_profile(str(msg[1]))
            elif cmd == "buffer_profile":
                gui.set_module_buffer_profile(
                    grab_strategy=int(msg[1]),
                    image_node_num=int(msg[2]),
                    output_queue_size=int(msg[3]),
                )
            elif cmd == "start_recording":
                gui.set_module_start_recording()
            elif cmd == "stop_recording":
                gui.set_module_stop_recording()
            elif cmd == "quit":
                gui.close()
                app.quit()
                return
        # Also forward log messages
        if hasattr(gui, "_module_log_lines"):
            while gui._module_log_lines:
                log_queue.put(gui._module_log_lines.pop(0))

    poll_timer = _QTimer()
    poll_timer.setInterval(200)
    poll_timer.timeout.connect(_poll_commands)
    poll_timer.start()

    app.exec()


class CameraProcessManager:
    """Manages the camera module child process."""

    def __init__(self) -> None:
        self._process: Optional[multiprocessing.Process] = None
        self._cmd_queue: Optional[multiprocessing.Queue] = None
        self._log_queue: Optional[multiprocessing.Queue] = None
        self._shared_frame_counter = multiprocessing.Value('i', 0)

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def launch(self, save_folder: str, base_name: str) -> None:
        if self.is_running:
            return
        self._cmd_queue = multiprocessing.Queue()
        self._log_queue = multiprocessing.Queue()
        self._shared_frame_counter.value = 0
        self._process = multiprocessing.Process(
            target=_camera_process_main,
            args=(save_folder, base_name, self._cmd_queue, self._log_queue,
                  self._shared_frame_counter),
            daemon=True,
        )
        self._process.start()

    def send_save_config(self, folder: str, base_name: str) -> None:
        if self._cmd_queue is not None:
            self._cmd_queue.put(("set_save", folder, base_name))

    def send_auto_start(self, enabled: bool) -> None:
        if self._cmd_queue is not None:
            self._cmd_queue.put(("auto_start", enabled))

    def send_sync_enabled(self, enabled: bool) -> None:
        if self._cmd_queue is not None:
            self._cmd_queue.put(("sync_enable", enabled))

    def send_sync_profile(self, output_line: str, source_mode: str, pulse_us: float) -> None:
        if self._cmd_queue is not None:
            self._cmd_queue.put(("sync_profile", output_line, source_mode, float(pulse_us)))

    def send_camera_profile(self, exposure_us: float, gain_db: float, fps: float, resolution: str) -> None:
        if self._cmd_queue is not None:
            self._cmd_queue.put(
                ("camera_profile", float(exposure_us), float(gain_db), float(fps), str(resolution))
            )

    def send_recording_profile(self, codec_index: int, queue_size: int, drop_if_full: bool) -> None:
        if self._cmd_queue is not None:
            self._cmd_queue.put(("recording_profile", int(codec_index), int(queue_size), bool(drop_if_full)))

    def send_trigger_profile(self, trigger_line: str) -> None:
        if self._cmd_queue is not None:
            self._cmd_queue.put(("trigger_profile", str(trigger_line)))

    def send_buffer_profile(self, grab_strategy: int, image_node_num: int, output_queue_size: int) -> None:
        if self._cmd_queue is not None:
            self._cmd_queue.put(
                ("buffer_profile", int(grab_strategy), int(image_node_num), int(output_queue_size))
            )

    def send_start_recording(self) -> None:
        if self._cmd_queue is not None:
            self._cmd_queue.put(("start_recording",))

    def send_stop_recording(self) -> None:
        if self._cmd_queue is not None:
            self._cmd_queue.put(("stop_recording",))

    def drain_logs(self) -> list:
        lines = []
        if self._log_queue is not None:
            while not self._log_queue.empty():
                try:
                    lines.append(self._log_queue.get_nowait())
                except Exception:
                    break
        return lines

    def quit(self) -> None:
        if self._cmd_queue is not None:
            try:
                self._cmd_queue.put(("quit",))
            except Exception:
                pass
        if self._process is not None:
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._process.terminate()
            self._process = None


# ---------------------------------------------------------------------------
# Advanced Settings Dialog
# ---------------------------------------------------------------------------

class AdvancedSettingsDialog(QDialog):
    def __init__(self, settings: dict, timebase_options: Optional[List[str]] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advanced Settings")
        self.settings = settings

        form = QFormLayout(self)

        self.device_edit = QLineEdit(settings.get("device_name", "Dev1"))
        self.microscope_pulse_spin = QDoubleSpinBox()
        self.microscope_pulse_spin.setRange(1.0, 10000.0)
        self.microscope_pulse_spin.setDecimals(1)
        self.microscope_pulse_spin.setSuffix(" ms")
        self.microscope_pulse_spin.setValue(
            settings.get("microscope_pulse_ms", 100.0)
        )

        self.camera_test_dur_spin = QDoubleSpinBox()
        self.camera_test_dur_spin.setRange(0.5, 60.0)
        self.camera_test_dur_spin.setDecimals(1)
        self.camera_test_dur_spin.setSuffix(" s")
        self.camera_test_dur_spin.setValue(
            settings.get("camera_test_duration_s", 5.0)
        )

        self.camera_exposure_spin = QDoubleSpinBox()
        self.camera_exposure_spin.setRange(10.0, 200000.0)
        self.camera_exposure_spin.setDecimals(1)
        self.camera_exposure_spin.setSuffix(" us")
        self.camera_exposure_spin.setValue(
            settings.get("camera_exposure_us", 5000.0)
        )

        self.camera_gain_spin = QDoubleSpinBox()
        self.camera_gain_spin.setRange(0.0, 30.0)
        self.camera_gain_spin.setDecimals(2)
        self.camera_gain_spin.setSuffix(" dB")
        self.camera_gain_spin.setValue(
            settings.get("camera_gain_db", 14.0)
        )

        self.camera_fps_spin = QDoubleSpinBox()
        self.camera_fps_spin.setRange(1.0, 500.0)
        self.camera_fps_spin.setDecimals(2)
        self.camera_fps_spin.setValue(
            settings.get("camera_fps", 60.0)
        )

        self.camera_resolution_edit = QLineEdit(
            str(settings.get("camera_resolution", "1440x1080"))
        )

        self.wheel_diam_spin = QDoubleSpinBox()
        self.wheel_diam_spin.setRange(0.1, 1000.0)
        self.wheel_diam_spin.setDecimals(3)
        self.wheel_diam_spin.setSuffix(" cm")
        self.wheel_diam_spin.setValue(
            settings.get("wheel_diameter_cm", 15.5)
        )

        self.ppr_spin = QSpinBox()
        self.ppr_spin.setRange(1, 100000)
        self.ppr_spin.setValue(settings.get("encoder_ppr", 1024))

        self.smoothing_spin = QDoubleSpinBox()
        self.smoothing_spin.setRange(0.001, 2.0)
        self.smoothing_spin.setDecimals(3)
        self.smoothing_spin.setSuffix(" s")
        self.smoothing_spin.setValue(
            settings.get("smoothing_window_s", 0.05)
        )

        self.silence_spin = QDoubleSpinBox()
        self.silence_spin.setRange(0.01, 10.0)
        self.silence_spin.setDecimals(3)
        self.silence_spin.setSuffix(" s")
        self.silence_spin.setValue(
            settings.get("silence_timeout_s", 0.3)
        )

        self.estimated_fps_spin = QDoubleSpinBox()
        self.estimated_fps_spin.setRange(1.0, 200000.0)
        self.estimated_fps_spin.setDecimals(1)
        self.estimated_fps_spin.setValue(
            settings.get("estimated_fps", 7000.0)
        )

        self.jitter_min_spin = QDoubleSpinBox()
        self.jitter_min_spin.setRange(-60000.0, 60000.0)
        self.jitter_min_spin.setDecimals(1)
        self.jitter_min_spin.setSuffix(" ms")
        self.jitter_min_spin.setValue(
            settings.get("jitter_min_ms", -1000.0)
        )

        self.jitter_max_spin = QDoubleSpinBox()
        self.jitter_max_spin.setRange(-60000.0, 60000.0)
        self.jitter_max_spin.setDecimals(1)
        self.jitter_max_spin.setSuffix(" ms")
        self.jitter_max_spin.setValue(
            settings.get("jitter_max_ms", 1000.0)
        )

        self.camera_sync_edge_combo = QLineEdit(
            settings.get("camera_sync_active_edge", "falling")
        )

        self.timebase_combo = QComboBox()
        options = timebase_options or ["20MHzTimebase"]
        self.timebase_combo.addItems(options)
        current_tb = str(settings.get("timebase_terminal", "20MHzTimebase"))
        idx_tb = self.timebase_combo.findText(current_tb)
        if idx_tb >= 0:
            self.timebase_combo.setCurrentIndex(idx_tb)

        form.addRow("NI Device", self.device_edit)
        form.addRow("Microscope pulse width", self.microscope_pulse_spin)
        form.addRow("Camera test trigger dur", self.camera_test_dur_spin)
        form.addRow("Camera exposure", self.camera_exposure_spin)
        form.addRow("Camera gain", self.camera_gain_spin)
        form.addRow("Camera frame rate", self.camera_fps_spin)
        form.addRow("Camera resolution", self.camera_resolution_edit)
        form.addRow("Wheel diameter", self.wheel_diam_spin)
        form.addRow("Encoder PPR", self.ppr_spin)
        form.addRow("Velocity smoothing window", self.smoothing_spin)
        form.addRow("Silence timeout", self.silence_spin)
        form.addRow("Estimated sync Hz", self.estimated_fps_spin)
        form.addRow("Event jitter min", self.jitter_min_spin)
        form.addRow("Event jitter max", self.jitter_max_spin)
        form.addRow("Counter timebase", self.timebase_combo)
        form.addRow("Camera sync active edge", self.camera_sync_edge_combo)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("Apply")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        form.addRow(btn_row)

    def apply_to_settings(self) -> dict:
        self.settings["device_name"] = self.device_edit.text().strip() or "Dev1"
        self.settings["microscope_pulse_ms"] = float(
            self.microscope_pulse_spin.value()
        )
        self.settings["camera_test_duration_s"] = float(
            self.camera_test_dur_spin.value()
        )
        self.settings["camera_exposure_us"] = float(self.camera_exposure_spin.value())
        self.settings["camera_gain_db"] = float(self.camera_gain_spin.value())
        self.settings["camera_fps"] = float(self.camera_fps_spin.value())
        self.settings["camera_resolution"] = (
            self.camera_resolution_edit.text().strip() or "1440x1080"
        )
        self.settings["wheel_diameter_cm"] = float(self.wheel_diam_spin.value())
        self.settings["encoder_ppr"] = int(self.ppr_spin.value())
        self.settings["smoothing_window_s"] = float(self.smoothing_spin.value())
        self.settings["silence_timeout_s"] = float(self.silence_spin.value())
        self.settings["estimated_fps"] = float(self.estimated_fps_spin.value())
        jitter_min_ms = float(self.jitter_min_spin.value())
        jitter_max_ms = float(self.jitter_max_spin.value())
        if jitter_min_ms > jitter_max_ms:
            jitter_min_ms, jitter_max_ms = jitter_max_ms, jitter_min_ms
        self.settings["jitter_min_ms"] = jitter_min_ms
        self.settings["jitter_max_ms"] = jitter_max_ms
        self.settings["timebase_terminal"] = self.timebase_combo.currentText().strip() or "20MHzTimebase"
        self.settings["camera_sync_active_edge"] = (
            self.camera_sync_edge_combo.text().strip() or "falling"
        )
        return self.settings


class AddInputDialog(QDialog):
    """Dialog to add a user-defined input line to the IO table."""

    def __init__(self, device_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Input")

        form = QFormLayout(self)

        self.kind_combo = QComboBox()
        self.kind_combo.addItems(["Digital", "Analog"])

        self.line_edit = QLineEdit(f"{device_name}/port0/line0")
        self.name_edit = QLineEdit("")

        note = QLabel("Sampling rate follows microscope frame sync.")
        note.setWordWrap(True)

        form.addRow("Input type", self.kind_combo)
        form.addRow("Line to listen", self.line_edit)
        form.addRow("Name (optional)", self.name_edit)
        form.addRow(note)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add")
        cancel_btn = QPushButton("Cancel")
        add_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(cancel_btn)
        form.addRow(btn_row)

    def get_values(self) -> tuple:
        input_kind = self.kind_combo.currentText().strip().lower()
        line = self.line_edit.text().strip()
        label = self.name_edit.text().strip() or line
        return input_kind, line, label


# ---------------------------------------------------------------------------
# Session Plot Dialog
# ---------------------------------------------------------------------------

class SessionPlotDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Session Viewer")
        self.resize(1000, 700)

        layout = QVBoxLayout(self)
        self.figure = Figure()
        self.canvas = FigureCanvas(self.figure)
        self.toolbar = NavigationToolbar(self.canvas, self)
        self.info_box = QTextEdit()
        self.info_box.setReadOnly(True)
        self.info_box.setMaximumHeight(90)

        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)
        layout.addWidget(self.info_box)

    @staticmethod
    def _safe_float(value):
        try:
            return float(value)
        except Exception:
            return None

    def plot_csv(self, file_path: str) -> None:
        with open(file_path, "r", newline="") as fobj:
            reader = csv.DictReader(fobj)
            fieldnames = reader.fieldnames or []
            rows = list(reader)

        if not rows:
            raise RuntimeError("CSV is empty.")
        if "Time_s" not in fieldnames:
            raise RuntimeError("CSV must contain 'Time_s' column.")

        time_vals = []
        pos_vals = []
        vel_vals = []
        for row in rows:
            t = self._safe_float(row.get("Time_s"))
            if t is None:
                continue
            time_vals.append(t)
            pos_vals.append(self._safe_float(row.get("Zeroed_Dist_cm")))
            vel_vals.append(self._safe_float(row.get("Smoothed_Vel_cm_s")))

        arduino_vals = []
        has_arduino_state = "Arduino_State" in fieldnames
        if has_arduino_state:
            for row in rows:
                arduino_vals.append(self._safe_float(row.get("Arduino_State")))

        if not time_vals:
            raise RuntimeError("No valid Time_s data.")

        self.figure.clear()
        ax_pos = self.figure.add_subplot(211)
        ax_vel = self.figure.add_subplot(212, sharex=ax_pos)

        pos_plot = [v if v is not None else float("nan") for v in pos_vals]
        vel_plot = [v if v is not None else float("nan") for v in vel_vals]

        ax_pos.plot(time_vals, pos_plot, linewidth=1.0, color="tab:blue")
        ax_pos.set_ylabel("Position (cm)")
        ax_pos.grid(True, alpha=0.3)

        ax_vel.plot(time_vals, vel_plot, linewidth=1.0, color="tab:orange")
        ax_vel.set_ylabel("Velocity (cm/s)")
        ax_vel.set_xlabel("Time (s)")
        ax_vel.grid(True, alpha=0.3)

        if has_arduino_state and arduino_vals:
            intervals = []
            in_high = False
            start_t = 0.0
            sample_count = min(len(time_vals), len(arduino_vals))
            min_visible_span_s = 0.03  # visualization-only minimum for short pulses
            for i in range(sample_count):
                val = arduino_vals[i]
                high = (val is not None) and (val >= 0.5)
                if high and not in_high:
                    in_high = True
                    start_t = time_vals[i]
                elif (not high) and in_high:
                    in_high = False
                    intervals.append((start_t, time_vals[i]))
            if in_high and sample_count > 0:
                intervals.append((start_t, time_vals[sample_count - 1]))

            drew_label = False
            t_min = time_vals[0]
            t_max = time_vals[sample_count - 1]
            for t0, t1 in intervals:
                if t1 <= t0:
                    continue
                span = t1 - t0
                if span < min_visible_span_s:
                    center = 0.5 * (t0 + t1)
                    half = 0.5 * min_visible_span_s
                    t0_plot = max(t_min, center - half)
                    t1_plot = min(t_max, center + half)
                else:
                    t0_plot, t1_plot = t0, t1
                ax_vel.hlines(
                    y=0.92,
                    xmin=t0_plot,
                    xmax=t1_plot,
                    transform=ax_vel.get_xaxis_transform(),
                    colors="tab:red",
                    linewidth=3.0,
                    label=("Arduino state" if not drew_label else None),
                )
                drew_label = True
            if drew_label:
                ax_vel.legend(loc="upper right")

        self.figure.tight_layout()
        self.canvas.draw()
        self.info_box.setPlainText(
            f"File: {file_path}\n"
            f"Rows: {len(rows)}\n"
            f"Duration: {time_vals[-1]:.3f} s"
        )


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class BehaviorHubWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BehaviorHub")
        self.resize(600, 680)

        self.save_dir: str = os.getcwd()
        self.last_saved_path: Optional[str] = None
        self.worker_thread: Optional[QThread] = None
        self.worker: Optional[NISessionWorker] = None
        self.camera_mgr = CameraProcessManager()

        # Advanced setting defaults
        self.adv_settings: dict = {
            "device_name": "Dev1",
            "microscope_pulse_ms": 100.0,
            "camera_test_duration_s": 5.0,
            "camera_exposure_us": 5000.0,
            "camera_gain_db": 14.0,
            "camera_fps": 60.0,
            "camera_resolution": "1440x1080",
            "camera_codec_profile_index": 0,
            "camera_writer_queue_size": 512,
            "camera_drop_if_full": True,
            "camera_trigger_input_line": "Line0",
            "camera_sync_output_line": "Line1",
            "camera_sync_source_mode": "Frame start active",
            "camera_sync_pulse_us": 1000.0,
            "camera_grab_strategy": 2,
            "camera_image_node_num": 32,
            "camera_output_queue_size": 8,
            "timebase_terminal": "20MHzTimebase",
            "timebase_freq_hz": 20_000_000.0,
            "wheel_diameter_cm": 15.5,
            "encoder_ppr": 1024,
            "smoothing_window_s": 0.05,
            "silence_timeout_s": 0.3,
            "estimated_fps": 7000.0,
            "jitter_min_ms": -1000.0,
            "jitter_max_ms": 1000.0,
            "camera_sync_active_edge": "falling",
        }

        # Test trigger state
        self._test_cam_task = None
        self._test_cam_timer: Optional[QTimer] = None
        self._test_events_running = False
        self._idle_speed_timer: Optional[QTimer] = None
        self._idle_speed_last_ticks: Optional[int] = None
        self._idle_speed_last_ts: Optional[float] = None
        self._idle_speed_start_ticks: Optional[int] = None
        self._idle_speed_history: deque = deque()
        self._idle_encoder_task = None

        self._init_ui()
        self._bind_signals()
        self._check_hardware()
        self._check_camera()
        self._start_idle_speed_monitor()

        # Poll camera process logs
        self._cam_log_timer = QTimer(self)
        self._cam_log_timer.setInterval(500)
        self._cam_log_timer.timeout.connect(self._poll_camera_logs)
        self._cam_log_timer.start()

    # ------------------------------------------------------------------ UI
    def _init_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)

        # ============ ROW 1 — three columns ============
        row1 = QHBoxLayout()

        # —— Column 1: Experiment ——
        exp_group = QGroupBox("Experiment")
        exp_layout = QGridLayout(exp_group)

        self.path_edit = QLineEdit(os.path.abspath(self.save_dir))
        self.path_edit.setReadOnly(True)
        self.browse_btn = QPushButton("Browse")

        self.animal_id_edit = QLineEdit("A35")
        self.suffix_edit = QLineEdit("puff_1")

        self.realtime_speed_label = QLabel("-- cm/s")
        self.realtime_speed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.frame_state_label = QLabel("Scope: 0 | Cam: 0")

        self.start_btn = QPushButton("Start")
        self.start_btn.setMinimumHeight(50)
        self.abort_btn = QPushButton("Abort")
        self.abort_btn.setMinimumHeight(50)
        self.abort_btn.hide()

        exp_layout.setColumnStretch(0, 0)
        exp_layout.setColumnStretch(1, 1)
        exp_layout.setColumnStretch(2, 1)
        exp_layout.setColumnStretch(3, 0)
        self.browse_btn.setFixedWidth(60)

        exp_layout.addWidget(QLabel("Path"), 0, 0)
        exp_layout.addWidget(self.path_edit, 0, 1, 1, 2)
        exp_layout.addWidget(self.browse_btn, 0, 3)
        exp_layout.addWidget(QLabel("ID"), 1, 0)
        exp_layout.addWidget(self.animal_id_edit, 1, 1)
        exp_layout.addWidget(QLabel("Suffix"), 1, 2)
        exp_layout.addWidget(self.suffix_edit, 1, 3)
        exp_layout.addWidget(self.frame_state_label, 2, 0, 1, 4)
        exp_layout.addWidget(QLabel("Speed"), 3, 0)
        exp_layout.addWidget(self.realtime_speed_label, 3, 1, 1, 3)
        exp_layout.addWidget(self.start_btn, 4, 0, 1, 4)
        exp_layout.addWidget(self.abort_btn, 4, 0, 1, 4)

        # —— Column 2: Hardware ——
        hw_group = QGroupBox("Hardware")
        hw_layout = QVBoxLayout(hw_group)

        self.microscope_sync_check = QCheckBox("Enable microscope sync")
        self.microscope_sync_check.setChecked(True)
        self.microscope_sync_check.setEnabled(True)
        self.test_microscope_btn = QPushButton("Test microscope trigger")

        self.camera_sync_check = QCheckBox("Enable camera sync")
        self.camera_sync_check.setChecked(True)
        self.auto_start_camera_check = QCheckBox("Auto-start camera")
        self.auto_start_camera_check.setChecked(False)
        self.test_camera_btn = QPushButton("Test camera trigger (5 s)")
        self.test_events_btn = QPushButton("Test events")

        hw_layout.addWidget(self.microscope_sync_check)
        hw_layout.addWidget(self.test_microscope_btn)
        hw_layout.addWidget(self.camera_sync_check)
        hw_layout.addWidget(self.auto_start_camera_check)
        hw_layout.addWidget(self.test_camera_btn)
        hw_layout.addWidget(self.test_events_btn)

        # —— Column 3: Software ——
        sw_group = QGroupBox("Software")
        sw_layout = QVBoxLayout(sw_group)

        self.camera_module_btn = QPushButton("Camera Module")
        self.camera_module_btn.setMinimumHeight(50)
        self.advanced_btn = QPushButton("Advanced Settings")
        self.save_config_btn = QPushButton("Save Config")
        self.load_config_btn = QPushButton("Load Config")
        self.view_latest_btn = QPushButton("View Latest")
        self.load_session_btn = QPushButton("Load Session")

        sw_layout.addWidget(self.camera_module_btn)
        sw_layout.addWidget(self.advanced_btn)
        cfg_row = QHBoxLayout()
        cfg_row.addWidget(self.save_config_btn)
        cfg_row.addWidget(self.load_config_btn)
        sw_layout.addLayout(cfg_row)
        view_row = QHBoxLayout()
        view_row.addWidget(self.view_latest_btn)
        view_row.addWidget(self.load_session_btn)
        sw_layout.addLayout(view_row)

        row1.addWidget(exp_group, 1)
        row1.addWidget(hw_group, 1)
        row1.addWidget(sw_group, 1)

        # ============ ROW 2 — IO Lines + Events in a tab widget ============
        self.io_evt_tabs = QTabWidget()

        # -- Events tab (shown by default, index 0) --
        evt_tab = QWidget()
        evt_layout = QVBoxLayout(evt_tab)

        self.evt_table = QTableWidget(0, 5)
        self.evt_table.setHorizontalHeaderLabels(
            ["Time (ms)", "Pin", "Pulse Width (ms)", "Jitter", "Remark"]
        )
        self.evt_table.horizontalHeader().setStretchLastSection(True)
        self.evt_table.setColumnWidth(3, 62)
        self.evt_table.setMinimumHeight(100)
        evt_layout.addWidget(self.evt_table)

        evt_btn_row = QHBoxLayout()
        self.evt_add_btn = QPushButton("Add Event")
        self.evt_del_btn = QPushButton("Delete Event")
        self.inp_add_btn = QPushButton("Add input")
        evt_btn_row.addWidget(self.evt_add_btn)
        evt_btn_row.addWidget(self.evt_del_btn)
        evt_btn_row.addWidget(self.inp_add_btn)
        evt_btn_row.addStretch(1)
        evt_layout.addLayout(evt_btn_row)

        # -- IO Lines tab --
        io_tab = QWidget()
        io_layout = QVBoxLayout(io_tab)

        self.io_table = QTableWidget(0, 3)
        self.io_table.setHorizontalHeaderLabels(["Pin", "Direction", "Function"])
        self.io_table.horizontalHeader().setStretchLastSection(True)
        self.io_table.setMinimumHeight(150)
        self.io_table.setColumnWidth(0, 220)  # wider pin column for long terminals
        io_layout.addWidget(self.io_table)

        self._populate_default_io_table()
        self._populate_default_events()

        self.io_evt_tabs.addTab(evt_tab, "Events")
        self.io_evt_tabs.addTab(io_tab, "IO Lines")
        self.io_evt_tabs.setCurrentIndex(0)  # Events by default

        # ============ ROW 4 — Log area ============
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(95)
        self.log_box.setStyleSheet("font-size: 11px;")

        root_layout.addLayout(row1)
        root_layout.addWidget(self.io_evt_tabs)
        root_layout.addWidget(self.log_box)

    def _populate_default_io_table(self) -> None:
        """Fill IO table with NI_V1.4_camera.py defaults."""
        defaults = [
            ("/Dev1/PFI0", "Input", "Frame_Clock"),
            ("Dev1/port0/line2", "Output", "Microscope_Start"),
            ("Dev1/port0/line3", "Input", "Arduino_State"),
            ("/Dev1/PFI4", "Input", "Encoder_A"),
            ("/Dev1/PFI5", "Input", "Encoder_B"),
            ("Dev1/port0/line14", "Input", "Camera_Frame"),
            ("Dev1/port0/line15", "Output", "Camera_Trigger"),
        ]
        for pin, direction, function in defaults:
            row = self.io_table.rowCount()
            self.io_table.insertRow(row)
            pin_item = QTableWidgetItem(pin)
            dir_item = QTableWidgetItem(direction)
            dir_item.setFlags(dir_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            func_item = QTableWidgetItem(function)
            func_item.setFlags(func_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.io_table.setItem(row, 0, pin_item)
            self.io_table.setItem(row, 1, dir_item)
            self.io_table.setItem(row, 2, func_item)
        self._sort_io_table_by_pin()

    def _sort_io_table_by_pin(self) -> None:
        """Sort IO rows by line index (PFI/line number), then by pin text."""
        import re

        def _pin_key(pin_text: str) -> tuple:
            text = (pin_text or "").strip()
            m = re.search(r"(?:pfi|line)\s*(\d+)", text, flags=re.IGNORECASE)
            line_num = int(m.group(1)) if m else 9999
            return (line_num, text.lower())

        rows = []
        for r in range(self.io_table.rowCount()):
            pin = self.io_table.item(r, 0).text() if self.io_table.item(r, 0) else ""
            direction = self.io_table.item(r, 1).text() if self.io_table.item(r, 1) else ""
            function = self.io_table.item(r, 2).text() if self.io_table.item(r, 2) else ""
            rows.append((pin, direction, function))

        rows.sort(key=lambda x: _pin_key(x[0]))

        self._syncing_tables = True
        self.io_table.setRowCount(0)
        for pin, direction, function in rows:
            row = self.io_table.rowCount()
            self.io_table.insertRow(row)
            pin_item = QTableWidgetItem(pin)
            dir_item = QTableWidgetItem(direction)
            dir_item.setFlags(dir_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            func_item = QTableWidgetItem(function)
            func_item.setFlags(func_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.io_table.setItem(row, 0, pin_item)
            self.io_table.setItem(row, 1, dir_item)
            self.io_table.setItem(row, 2, func_item)
        self._syncing_tables = False

    def _update_io_table_device(self, new_dev: str) -> None:
        """Rewrite all pin strings in the IO table to use *new_dev*."""
        import re
        for row in range(self.io_table.rowCount()):
            pin_item = self.io_table.item(row, 0)
            if pin_item is None:
                continue
            old = pin_item.text()
            # Replace device name in paths like /Dev1/PFI0 or Dev1/port0/line2
            new = re.sub(r'Dev\d+', new_dev, old)
            if new != old:
                pin_item.setText(new)
        # Also update event pins
        for row in range(self.evt_table.rowCount()):
            pin_item = self.evt_table.item(row, 1)
            if pin_item is None:
                continue
            old = pin_item.text()
            new = re.sub(r'Dev\d+', new_dev, old)
            if new != old:
                pin_item.setText(new)
        self._sort_io_table_by_pin()

    # ---- Events ↔ IO table bidirectional sync ----
    _syncing_tables: bool = False  # re-entrancy guard

    def _populate_default_events(self) -> None:
        """Add default behavioural events matching the original Arduino timing."""
        dev = self.adv_settings.get("device_name", "Dev1")
        defaults = [
            (2300.0, f"{dev}/port0/line6", 5.0, "Buzz"),
            (2500.0, f"{dev}/port0/line7", 5.0, "Puff"),
        ]
        for time_ms, pin, pw_ms, remark in defaults:
            self._add_event_row(time_ms, pin, pw_ms, remark)

    def _add_event_row(
        self,
        time_ms: float = 0.0,
        pin: str = "",
        pw_ms: float = 5.0,
        remark: str = "",
        jitter_enabled: bool = False,
    ) -> None:
        self._syncing_tables = True
        row = self.evt_table.rowCount()
        self.evt_table.insertRow(row)
        self.evt_table.setItem(row, 0, QTableWidgetItem(str(time_ms)))
        self.evt_table.setItem(row, 1, QTableWidgetItem(pin))
        self.evt_table.setItem(row, 2, QTableWidgetItem(str(pw_ms)))
        jitter_item = QTableWidgetItem()
        jitter_item.setFlags(
            (jitter_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            & ~Qt.ItemFlag.ItemIsEditable
        )
        jitter_item.setCheckState(
            Qt.CheckState.Checked if jitter_enabled else Qt.CheckState.Unchecked
        )
        self.evt_table.setItem(row, 3, jitter_item)
        self.evt_table.setItem(row, 4, QTableWidgetItem(remark))
        self._syncing_tables = False
        # Ensure a matching IO row exists
        if pin:
            self._ensure_io_row_for_event(pin, remark)

    def _on_add_event(self) -> None:
        dev = self.adv_settings.get("device_name", "Dev1")
        self._add_event_row(0.0, f"{dev}/port0/line7", 5.0, "")

    def _on_add_input(self) -> None:
        dev = self.adv_settings.get("device_name", "Dev1")
        dlg = AddInputDialog(device_name=dev, parent=self)
        if not dlg.exec():
            return

        input_kind, pin, label = dlg.get_values()
        if not pin:
            QMessageBox.warning(self, "Add input", "Line to listen cannot be empty.")
            return

        # Use a sensible default analog terminal if the user switched type
        # but kept the default digital-style path.
        if input_kind == "analog" and "/ai" not in pin.lower():
            pin = f"{dev}/ai0"
            if not label or label == dlg.line_edit.text().strip():
                label = pin

        conflict_row = self._find_io_pin_conflict(pin)
        if conflict_row is not None:
            conflict_func = self.io_table.item(conflict_row, 2)
            conflict_name = conflict_func.text().strip() if conflict_func else f"row {conflict_row + 1}"
            QMessageBox.warning(
                self,
                "Add input",
                f"Line '{pin}' conflicts with existing line '{conflict_name}'.",
            )
            return

        # Avoid duplicate function names in CSV headers.
        existing_funcs = set()
        for r in range(self.io_table.rowCount()):
            it = self.io_table.item(r, 2)
            if it:
                existing_funcs.add(it.text().strip())
        base_label = label
        suffix = 2
        while label in existing_funcs:
            label = f"{base_label}_{suffix}"
            suffix += 1

        row = self.io_table.rowCount()
        self.io_table.insertRow(row)
        self.io_table.setItem(row, 0, QTableWidgetItem(pin))

        dir_text = "Input (Analog)" if input_kind == "analog" else "Input"
        dir_item = QTableWidgetItem(dir_text)
        dir_item.setFlags(dir_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.io_table.setItem(row, 1, dir_item)

        func_item = QTableWidgetItem(label)
        func_item.setFlags(func_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.io_table.setItem(row, 2, func_item)

        kind_note = "analog" if input_kind == "analog" else "digital"
        self.log("GUI", f"Added {kind_note} input: {label} ({pin})")

    def _on_delete_event(self) -> None:
        row = self.evt_table.currentRow()
        if row >= 0:
            # Get the pin before removing so we can clean up the IO row
            pin_item = self.evt_table.item(row, 1)
            old_pin = pin_item.text().strip() if pin_item else ""
            self.evt_table.removeRow(row)
            # If no other event uses that pin, remove the IO row too
            if old_pin and not self._event_uses_pin(old_pin):
                self._remove_io_row_by_pin(old_pin)

    def _event_uses_pin(self, pin: str) -> bool:
        """Check whether any event row still references *pin*."""
        for r in range(self.evt_table.rowCount()):
            it = self.evt_table.item(r, 1)
            if it and it.text().strip() == pin:
                return True
        return False

    def _ensure_io_row_for_event(self, pin: str, remark: str) -> None:
        """Create an IO-table Output row for an event pin if it doesn't exist."""
        self._syncing_tables = True
        for r in range(self.io_table.rowCount()):
            io_pin = self.io_table.item(r, 0)
            if io_pin and io_pin.text().strip() == pin:
                self._syncing_tables = False
                return
        # Not found → add new row with remark as function label
        func = remark or pin
        row = self.io_table.rowCount()
        self.io_table.insertRow(row)
        pin_item = QTableWidgetItem(pin)
        dir_item = QTableWidgetItem("Output")
        dir_item.setFlags(dir_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        func_item = QTableWidgetItem(func)
        func_item.setFlags(func_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.io_table.setItem(row, 0, pin_item)
        self.io_table.setItem(row, 1, dir_item)
        self.io_table.setItem(row, 2, func_item)
        self._syncing_tables = False
        self._sort_io_table_by_pin()

    def _sync_io_function_from_events(self, pin: str, preferred_row: Optional[int] = None) -> None:
        """Update IO Function text for *pin* from event remarks.

        If the edited row has a non-empty remark, prefer it. Otherwise,
        fall back to the latest non-empty remark among rows sharing the pin.
        If no remark exists, use the pin string.
        """
        if not pin:
            return

        preferred_remark = ""
        if preferred_row is not None:
            p_pin_item = self.evt_table.item(preferred_row, 1)
            p_remark_item = self.evt_table.item(preferred_row, 4)
            p_pin = p_pin_item.text().strip() if p_pin_item else ""
            if p_pin == pin and p_remark_item:
                preferred_remark = p_remark_item.text().strip()

        resolved_func = preferred_remark
        if not resolved_func:
            for r in range(self.evt_table.rowCount() - 1, -1, -1):
                p_item = self.evt_table.item(r, 1)
                if not p_item or p_item.text().strip() != pin:
                    continue
                rem_item = self.evt_table.item(r, 4)
                rem = rem_item.text().strip() if rem_item else ""
                if rem:
                    resolved_func = rem
                    break
        if not resolved_func:
            resolved_func = pin

        self._syncing_tables = True
        for r in range(self.io_table.rowCount()):
            io_pin_item = self.io_table.item(r, 0)
            if io_pin_item and io_pin_item.text().strip() == pin:
                func_item = self.io_table.item(r, 2)
                if func_item and func_item.text() != resolved_func:
                    func_item.setText(resolved_func)
                break
        self._syncing_tables = False

    def _remove_io_row_by_pin(self, pin: str) -> None:
        """Remove an IO-table row matching *pin* (only event-generated Output rows)."""
        # Don't remove "system" rows like Frame_Clock etc.
        system_functions = {
            "Frame_Clock", "Microscope_Start", "Arduino_State",
            "Encoder_A", "Encoder_B", "Camera_Frame", "Camera_Trigger",
        }
        self._syncing_tables = True
        for r in range(self.io_table.rowCount()):
            io_pin = self.io_table.item(r, 0)
            func_item = self.io_table.item(r, 2)
            if io_pin and io_pin.text().strip() == pin:
                func = func_item.text().strip() if func_item else ""
                if func not in system_functions:
                    self.io_table.removeRow(r)
                    break
        self._syncing_tables = False

    def _cleanup_orphaned_io_rows(self) -> None:
        """Remove IO Output rows whose pin no longer appears in any event."""
        system_functions = {
            "Frame_Clock", "Microscope_Start", "Arduino_State",
            "Encoder_A", "Encoder_B", "Camera_Frame", "Camera_Trigger",
        }
        event_pins = set()
        for r in range(self.evt_table.rowCount()):
            it = self.evt_table.item(r, 1)
            if it:
                p = it.text().strip()
                if p:
                    event_pins.add(p)
        self._syncing_tables = True
        rows_to_remove = []
        for r in range(self.io_table.rowCount()):
            dir_item = self.io_table.item(r, 1)
            if dir_item is None or dir_item.text() != "Output":
                continue
            func_item = self.io_table.item(r, 2)
            func = func_item.text().strip() if func_item else ""
            if func in system_functions:
                continue
            pin_item = self.io_table.item(r, 0)
            pin = pin_item.text().strip() if pin_item else ""
            if pin and pin not in event_pins:
                rows_to_remove.append(r)
        for r in reversed(rows_to_remove):
            self.io_table.removeRow(r)
        self._syncing_tables = False

    def _on_evt_table_changed(self, row: int, col: int) -> None:
        """Event table cell edited → sync to IO table."""
        if self._syncing_tables:
            return
        if col in (1, 4):
            # Pin/remark changed — keep IO rows in sync
            pin = (self.evt_table.item(row, 1).text().strip()
                   if self.evt_table.item(row, 1) else "")
            remark = (self.evt_table.item(row, 4).text().strip()
                      if self.evt_table.item(row, 4) else "")
            if pin:
                io_row = self._find_io_pin_conflict(pin)
                if io_row is not None:
                    dir_item = self.io_table.item(io_row, 1)
                    direction = dir_item.text().strip() if dir_item else ""
                    if direction.startswith("Input"):
                        self.log("WARN", f"Event pin '{pin}' conflicts with existing input line")
                        self._syncing_tables = True
                        pin_item = self.evt_table.item(row, 1)
                        if pin_item is not None:
                            pin_item.setText("")
                        self._syncing_tables = False
                        return
                self._ensure_io_row_for_event(pin, remark)
                self._sync_io_function_from_events(pin, preferred_row=row)
            self._cleanup_orphaned_io_rows()

    def _on_io_table_changed(self, row: int, col: int) -> None:
        """IO table pin cell edited → sync back to event table."""
        if self._syncing_tables:
            return
        if col != 0:
            return  # Only pin column is editable
        new_pin = (self.io_table.item(row, 0).text().strip()
                   if self.io_table.item(row, 0) else "")
        func_item = self.io_table.item(row, 2)
        func = func_item.text().strip() if func_item else ""
        if not func:
            return

        # Deleting pin text removes user-added input rows.
        if not new_pin:
            dir_item = self.io_table.item(row, 1)
            direction = dir_item.text().strip() if dir_item else ""
            protected = {
                "Frame_Clock", "Microscope_Start", "Arduino_State",
                "Encoder_A", "Encoder_B", "Camera_Frame", "Camera_Trigger",
            }
            if direction.startswith("Input") and func not in protected:
                self._syncing_tables = True
                self.io_table.removeRow(row)
                self._syncing_tables = False
                self.log("GUI", f"Removed input: {func}")
            return

        conflict_row = self._find_io_pin_conflict(new_pin, current_row=row)
        if conflict_row is not None:
            conflict_func = self.io_table.item(conflict_row, 2)
            conflict_name = conflict_func.text().strip() if conflict_func else f"row {conflict_row + 1}"
            self.log("WARN", f"Line conflict: '{new_pin}' already used by '{conflict_name}'")
            self._syncing_tables = True
            self.io_table.item(row, 0).setText("")
            self._syncing_tables = False
            return

        # Find event rows whose remark matches this IO row's function
        self._syncing_tables = True
        for r in range(self.evt_table.rowCount()):
            evt_remark = (self.evt_table.item(r, 4).text().strip()
                          if self.evt_table.item(r, 4) else "")
            if evt_remark == func:
                evt_pin_item = self.evt_table.item(r, 1)
                if evt_pin_item and evt_pin_item.text().strip() != new_pin:
                    evt_pin_item.setText(new_pin)
        self._syncing_tables = False

    def _build_events_list(self, apply_jitter: bool = False) -> List[EventDef]:
        """Read the Events table into a list of EventDef."""
        events: List[EventDef] = []
        jitter_min_ms = float(self.adv_settings.get("jitter_min_ms", -1000.0))
        jitter_max_ms = float(self.adv_settings.get("jitter_max_ms", 1000.0))
        if jitter_min_ms > jitter_max_ms:
            jitter_min_ms, jitter_max_ms = jitter_max_ms, jitter_min_ms
        for row in range(self.evt_table.rowCount()):
            try:
                time_ms = float(self.evt_table.item(row, 0).text())
                pin = self.evt_table.item(row, 1).text().strip()
                pw_ms = float(self.evt_table.item(row, 2).text())
                jitter_item = self.evt_table.item(row, 3)
                jitter_enabled = bool(
                    jitter_item
                    and jitter_item.checkState() == Qt.CheckState.Checked
                )
                remark = (self.evt_table.item(row, 4).text().strip()
                          if self.evt_table.item(row, 4) else "")
            except (ValueError, AttributeError):
                continue
            if pin:
                actual_time_ms = time_ms
                if apply_jitter and jitter_enabled:
                    jitter_ms = random.uniform(jitter_min_ms, jitter_max_ms)
                    jittered = time_ms + jitter_ms
                    actual_time_ms = max(0.0, jittered)
                    if actual_time_ms != jittered:
                        self.log(
                            "NI",
                            f"Event '{remark or pin}' jitter clipped at 0 ms (offset {jitter_ms:+.1f} ms)",
                        )
                    else:
                        self.log(
                            "NI",
                            f"Event '{remark or pin}' jitter applied: {jitter_ms:+.1f} ms -> {actual_time_ms:.1f} ms",
                        )
                events.append(EventDef(
                    time_ms=actual_time_ms,
                    pin=pin,
                    pulse_width_ms=pw_ms,
                    jitter_enabled=jitter_enabled,
                    remark=remark,
                ))
        return events

    def _check_event_overlaps(self, events: List[EventDef]) -> None:
        """Log warnings for events on the same pin whose time ranges overlap."""
        from collections import defaultdict
        by_pin: dict = defaultdict(list)
        for ev in events:
            by_pin[ev.pin].append(ev)
        for pin, evts in by_pin.items():
            sorted_evts = sorted(evts, key=lambda e: e.time_ms)
            for i in range(len(sorted_evts) - 1):
                a = sorted_evts[i]
                b = sorted_evts[i + 1]
                a_end = a.time_ms + a.pulse_width_ms
                if a_end > b.time_ms:
                    self.log(
                        "WARN",
                        f"Event overlap on {pin}: "
                        f"'{a.remark}' @{a.time_ms}ms (end {a_end:.1f}ms) "
                        f"overlaps '{b.remark}' @{b.time_ms}ms",
                    )

    def _bind_signals(self) -> None:
        self.browse_btn.clicked.connect(self._on_browse)
        self.start_btn.clicked.connect(self._on_start)
        self.abort_btn.clicked.connect(self._on_abort)
        self.test_microscope_btn.clicked.connect(self._on_test_microscope)
        self.test_camera_btn.clicked.connect(self._on_test_camera)
        self.test_events_btn.clicked.connect(self._on_test_events)
        self.camera_module_btn.clicked.connect(self._on_open_camera)
        self.advanced_btn.clicked.connect(self._on_advanced)
        self.save_config_btn.clicked.connect(self._on_save_config)
        self.load_config_btn.clicked.connect(self._on_load_config)
        self.view_latest_btn.clicked.connect(self._on_view_latest)
        self.load_session_btn.clicked.connect(self._on_load_session)
        self.camera_sync_check.toggled.connect(self._on_camera_sync_toggled)
        self.evt_add_btn.clicked.connect(self._on_add_event)
        self.evt_del_btn.clicked.connect(self._on_delete_event)
        self.inp_add_btn.clicked.connect(self._on_add_input)
        self.evt_table.cellChanged.connect(self._on_evt_table_changed)
        self.io_table.cellChanged.connect(self._on_io_table_changed)

    # ------------------------------------------------------------------ Log
    def log(self, category: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.append(f"[{ts}] [{category}] {message}")

    # --------------------------------------------------------- IO table reads
    def _io_pin(self, function: str) -> str:
        """Lookup a pin from the IO table by function name."""
        for row in range(self.io_table.rowCount()):
            func_item = self.io_table.item(row, 2)
            if func_item and func_item.text() == function:
                pin_item = self.io_table.item(row, 0)
                return pin_item.text().strip() if pin_item else ""
        return ""

    def _io_input_channels(self) -> List[IOLine]:
        """Return all *Input* direction lines from the IO table.

        Lines whose function names are system-level (Frame_Clock,
        Encoder_A, Encoder_B) are excluded because they have
        dedicated NI tasks.
        """
        excluded = {"Frame_Clock", "Encoder_A", "Encoder_B"}
        builtin = {"Arduino_State", "Camera_Frame"}
        builtin_channels: List[IOLine] = []
        custom_channels: List[IOLine] = []
        for row in range(self.io_table.rowCount()):
            dir_item = self.io_table.item(row, 1)
            if dir_item is None or not dir_item.text().startswith("Input"):
                continue
            func_item = self.io_table.item(row, 2)
            if func_item is None:
                continue
            func = func_item.text().strip()
            if func in excluded:
                continue
            pin_item = self.io_table.item(row, 0)
            pin = pin_item.text().strip() if pin_item else ""
            if not pin:
                continue
            lower_pin = pin.lower().replace(" ", "")
            lower_dir = dir_item.text().strip().lower()
            input_kind = "analog" if ("analog" in lower_dir or "/ai" in lower_pin) else "digital"
            ch = IOLine(pin=pin, direction="Input", function=func, input_kind=input_kind)
            if func in builtin:
                builtin_channels.append(ch)
            else:
                custom_channels.append(ch)
        return builtin_channels + custom_channels

    @staticmethod
    def _normalize_line_key(pin: str) -> str:
        text = (pin or "").strip().lower().replace(" ", "")
        if text.startswith("/"):
            text = text[1:]
        return text

    def _find_io_pin_conflict(self, pin: str, current_row: Optional[int] = None) -> Optional[int]:
        """Return row index of conflicting pin, excluding *current_row* if provided."""
        key = self._normalize_line_key(pin)
        if not key:
            return None
        for r in range(self.io_table.rowCount()):
            if current_row is not None and r == current_row:
                continue
            pin_item = self.io_table.item(r, 0)
            other_key = self._normalize_line_key(pin_item.text() if pin_item else "")
            if other_key and other_key == key:
                return r
        return None

    def _validate_unique_io_lines(self) -> bool:
        """Validate that all configured IO lines are unique."""
        seen = {}
        conflicts = []
        for r in range(self.io_table.rowCount()):
            pin_item = self.io_table.item(r, 0)
            pin_text = pin_item.text().strip() if pin_item else ""
            key = self._normalize_line_key(pin_text)
            if not key:
                continue
            if key in seen:
                conflicts.append((seen[key], r, pin_text))
            else:
                seen[key] = r
        if not conflicts:
            return True

        a, b, pin_text = conflicts[0]
        self.log("WARN", f"IO line conflict detected: '{pin_text}' used in rows {a + 1} and {b + 1}")
        QMessageBox.warning(
            self,
            "IO line conflict",
            f"Line '{pin_text}' is duplicated in IO table (rows {a + 1} and {b + 1}).",
        )
        return False

    # ------------------------------------------------------- File naming
    def _build_base_name(self) -> str:
        animal = (self.animal_id_edit.text().strip() or "Mouse").replace(" ", "_")
        suffix = (self.suffix_edit.text().strip() or "session").replace(" ", "_")
        return f"{animal}_{suffix}"

    def _build_csv_filename(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{self._build_base_name()}_{ts}.csv"

    # ------------------------------------------------------- Hardware check
    # Preferred NI product keywords (first match wins).
    _PREFERRED_NI_PRODUCTS = ["USB-6421", "USB-6341", "PCIe-6341", "PCI-6341"]
    # Device types to never use (e.g. Femtonics AOD controller).
    _BLOCKED_NI_PRODUCTS = ["6537"]

    def _get_timebase_options(self, device_name: str) -> List[str]:
        """Return available *Timebase terminals for a device (fallback safe list)."""
        fallback = ["20MHzTimebase", "100kHzTimebase"]
        try:
            from nidaqmx.system import System

            dev = next((d for d in System.local().devices if d.name == device_name), None)
            if dev is None:
                return fallback

            options: List[str] = []
            for term in getattr(dev, "terminals", []):
                text = str(term)
                if "Timebase" not in text:
                    continue
                suffix = text.split(f"/{device_name}/")[-1] if f"/{device_name}/" in text else text
                if suffix and suffix not in options:
                    options.append(suffix)
            return options or fallback
        except Exception:
            return fallback

    @staticmethod
    def _timebase_freq_guess(timebase_terminal: str) -> float:
        """Estimate numeric frequency from NI timebase terminal name."""
        text = (timebase_terminal or "").lower()
        if "100mhz" in text:
            return 100_000_000.0
        if "20mhz" in text:
            return 20_000_000.0
        if "10mhz" in text:
            return 10_000_000.0
        if "1mhz" in text:
            return 1_000_000.0
        if "100khz" in text:
            return 100_000.0
        return 20_000_000.0

    def _check_hardware(self) -> None:
        try:
            from nidaqmx.system import System

            sys_devices = System.local().devices
            device_info = [(d.name, getattr(d, "product_type", "")) for d in sys_devices]
            if not device_info:
                self.log("NI", "No NI devices found")
                return

            # Filter out blocked devices (e.g. Femtonics PCIe-6537B)
            usable = [
                (n, t) for n, t in device_info
                if not any(blk in t for blk in self._BLOCKED_NI_PRODUCTS)
            ]
            blocked = [
                (n, t) for n, t in device_info
                if any(blk in t for blk in self._BLOCKED_NI_PRODUCTS)
            ]
            for bname, btype in blocked:
                self.log("NI", f"Ignoring {bname} ({btype}) — blocklisted")

            if not usable:
                self.log("NI", "No usable NI devices (all blocklisted)")
                return

            # Try to auto-select a preferred device
            selected = None
            for keyword in self._PREFERRED_NI_PRODUCTS:
                for dev_name, prod_type in usable:
                    if keyword.lower() in prod_type.lower():
                        selected = dev_name
                        break
                if selected:
                    break

            if selected and selected != self.adv_settings["device_name"]:
                self.adv_settings["device_name"] = selected
                self._update_io_table_device(selected)
                self.log("NI", f"Auto-selected {selected} ({dict(device_info).get(selected, '')})")
            else:
                dev = self.adv_settings["device_name"]
                names = [d for d, _ in usable]
                if dev in names:
                    prod = dict(usable).get(dev, "")
                    self.log("NI", f"{dev} detected ({prod})" if prod else f"{dev} detected")
                else:
                    self.log(
                        "NI",
                        f"Device {dev} not found. Usable: {', '.join(f'{n} ({t})' for n, t in usable)}",
                    )
        except Exception as exc:
            self.log("NI", f"Hardware check failed: {exc}")

    def _check_camera(self) -> None:
        """Detect cameras at startup and report to log."""
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from camera_GUI import HikMVSCameraBackend, OpenCVCameraBackend

            hik = HikMVSCameraBackend()
            if hik.is_available():
                devs = hik.list_devices()
                if devs:
                    names = ", ".join(name for _, name in devs)
                    self.log("CAM", f"HikMVS: {len(devs)} camera(s) — {names}")
                else:
                    self.log("CAM", "HikMVS SDK ready but no camera found")
                hik.finalize()
            else:
                ocv = OpenCVCameraBackend()
                devs = ocv.list_devices()
                if devs:
                    self.log("CAM", f"OpenCV fallback: {len(devs)} camera(s)")
                else:
                    self.log("CAM", "No camera detected")
                err = hik.get_init_error().strip()
                if err:
                    self.log("CAM", f"HikMVS unavailable: {err}")
        except Exception as exc:
            self.log("CAM", f"Camera detection failed: {exc}")

    # ------------------------------------------------------- Build config
    def _build_session_config(self) -> SessionConfig:
        adv = self.adv_settings
        dev = adv["device_name"]
        camera_sync_line = self._io_pin("Camera_Frame") or f"{dev}/port0/line14"
        camera_sync_counter_pin = _normalize_counter_terminal(camera_sync_line, dev)
        if self.camera_sync_check.isChecked() and not camera_sync_counter_pin:
            self.log(
                "NI",
                f"Camera_Frame pin '{camera_sync_line}' is not counter-compatible; using sampled DI edge detection.",
            )

        save_path = os.path.abspath(
            os.path.join(self.save_dir, self._build_csv_filename())
        )

        cfg = SessionConfig(
            save_path=save_path,
            device_name=dev,
            frame_clock_pin=self._io_pin("Frame_Clock") or f"/{dev}/PFI0",
            microscope_start_line=self._io_pin("Microscope_Start")
            or f"{dev}/port0/line2",
            arduino_input_line=self._io_pin("Arduino_State")
            or f"{dev}/port0/line3",
            encoder_a_pfi=self._io_pin("Encoder_A") or f"/{dev}/PFI4",
            encoder_b_pfi=self._io_pin("Encoder_B") or f"/{dev}/PFI5",
            camera_sync_line=camera_sync_line,
            camera_sync_counter_pin=camera_sync_counter_pin,
            camera_trigger_pin=self._io_pin("Camera_Trigger")
            or f"{dev}/port0/line15",
            buzz_cmd_pin=f"{dev}/port0/line6",
            puff_cmd_pin=f"{dev}/port0/line7",
            time_counter=f"{dev}/ctr0",
            camera_sync_counter=f"{dev}/ctr1",
            encoder_counter=f"{dev}/ctr2",
            internal_timebase=(
                self.adv_settings.get("timebase_terminal", "20MHzTimebase")
                if str(self.adv_settings.get("timebase_terminal", "")).startswith("/")
                else f"/{dev}/{self.adv_settings.get('timebase_terminal', '20MHzTimebase')}"
            ),
            timebase_freq=float(self.adv_settings.get("timebase_freq_hz", 20_000_000.0)),
            estimated_fps=adv["estimated_fps"],
            wheel_diameter_cm=adv["wheel_diameter_cm"],
            encoder_ppr=adv["encoder_ppr"],
            smoothing_window_s=adv["smoothing_window_s"],
            silence_timeout_s=adv["silence_timeout_s"],
            microscope_pulse_s=adv["microscope_pulse_ms"] / 1000.0,
            camera_sync_active_edge=adv["camera_sync_active_edge"],
            enable_camera_sync=self.camera_sync_check.isChecked(),
            enable_microscope_sync=self.microscope_sync_check.isChecked(),
            input_channels=self._io_input_channels(),
            events=self._build_events_list(apply_jitter=True),
        )
        return cfg

    # ================================================ Slot implementations
    def _on_browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select save folder", self.save_dir
        )
        if folder:
            self.save_dir = folder
            self.path_edit.setText(os.path.abspath(folder))
            self.log("GUI", f"Save folder: {folder}")
            if self.camera_mgr.is_running:
                self.camera_mgr.send_save_config(folder, self._build_base_name())

    # ----------------------- Session start / abort
    def _on_start(self) -> None:
        if not self._validate_unique_io_lines():
            return
        try:
            cfg = self._build_session_config()
        except Exception as exc:
            QMessageBox.critical(self, "Config error", str(exc))
            self.log("GUI", f"Config error: {exc}")
            return

        self.last_saved_path = cfg.save_path
        self.log("GUI", f"Output: {os.path.basename(cfg.save_path)}")

        # Validate events: warn about overlaps on the same pin
        self._check_event_overlaps(cfg.events)

        self._stop_idle_speed_monitor()

        # Camera auto-start
        if self.auto_start_camera_check.isChecked():
            if not self.camera_mgr.is_running:
                self.camera_mgr.launch(self.save_dir, self._build_base_name())
                self.log("CAM", "Camera module auto-launched for recording")
            self.camera_mgr.send_save_config(
                self.save_dir, self._build_base_name()
            )
            self._push_camera_profile()
            self.camera_mgr.send_auto_start(True)
            self.log("CAM", "Auto-start enabled via master")
        elif self.camera_mgr.is_running:
            # Camera module open but auto-start off: keep camera idle.
            self.camera_mgr.send_save_config(
                self.save_dir, self._build_base_name()
            )
            self._push_camera_profile()
            self.camera_mgr.send_auto_start(False)
            self.log("CAM", "Auto-start disabled; camera recording not started")

        # Swap buttons — disable start to prevent double-click
        self.start_btn.setEnabled(False)
        self.start_btn.hide()
        self.abort_btn.show()

        # Create worker
        self.worker_thread = QThread()
        shared_counter = self.camera_mgr._shared_frame_counter if self.camera_mgr.is_running else None
        self.worker = NISessionWorker(cfg, shared_cam_counter=shared_counter)
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.velocity.connect(self._on_velocity)
        self.worker.frame_count.connect(self._on_frame_count)
        self.worker.camera_frame_count.connect(self._on_camera_frame_count)
        self.worker.status.connect(lambda msg: self.log("NI", msg))
        self.worker.error.connect(self._on_worker_error)
        self.worker.finished.connect(self._on_worker_finished)

        self.worker.error.connect(self._cleanup_worker)
        self.worker.finished.connect(self._cleanup_worker)
        self.worker_thread.start()

    def _on_abort(self) -> None:
        if self.worker is not None:
            self.worker.request_stop()
            self.log("NI", "Aborting session...")

    def _on_velocity(self, value: float) -> None:
        self.realtime_speed_label.setText(f"{value:.4f} cm/s")

    _scope_frames = 0
    _cam_frames = 0

    def _on_frame_count(self, count: int) -> None:
        self._scope_frames = count
        self.frame_state_label.setText(
            f"Scope: {self._scope_frames} | Cam: {self._cam_frames}"
        )

    def _on_camera_frame_count(self, count: int) -> None:
        self._cam_frames = count
        self.frame_state_label.setText(
            f"Scope: {self._scope_frames} | Cam: {self._cam_frames}"
        )

    def _on_worker_error(self, message: str) -> None:
        QMessageBox.critical(self, "Session error", message)
        self.log("NI", f"ERROR: {message}")

    def _on_worker_finished(self, message: str) -> None:
        self.log("NI", message)
        if self.camera_mgr.is_running:
            if self.auto_start_camera_check.isChecked():
                self.camera_mgr.send_auto_start(False)
            else:
                self.camera_mgr.send_stop_recording()
        self._start_idle_speed_monitor()

    def _cleanup_worker(self) -> None:
        self.abort_btn.hide()
        self.start_btn.show()
        self.start_btn.setEnabled(True)
        if self.worker_thread is not None:
            self.worker_thread.quit()
            self.worker_thread.wait(2000)
            self.worker_thread = None
        self.worker = None
        self._start_idle_speed_monitor()

    # ----------------------- Test triggers
    def _on_test_microscope(self) -> None:
        pin = self._io_pin("Microscope_Start")
        if not pin:
            self.log("NI", "Microscope_Start pin not configured")
            return
        pulse_s = self.adv_settings["microscope_pulse_ms"] / 1000.0
        try:
            with nidaqmx.Task() as t:
                t.do_channels.add_do_chan(pin)
                t.write(False)
                t.write(True)
                time.sleep(max(0.001, pulse_s))
                t.write(False)
            self.log(
                "NI",
                f"Microscope trigger sent ({self.adv_settings['microscope_pulse_ms']:.0f} ms) on {pin}",
            )
        except Exception as exc:
            self.log("NI", f"Microscope test failed: {exc}")

    def _on_test_camera(self) -> None:
        pin = self._io_pin("Camera_Trigger")
        if not pin:
            self.log("NI", "Camera_Trigger pin not configured")
            return
        if self._test_cam_task is not None:
            self.log("NI", "Camera test already in progress")
            return

        if self.auto_start_camera_check.isChecked():
            if not self.camera_mgr.is_running:
                self.log(
                    "CAM",
                    "Camera test skipped: open Camera Module first when auto-start recording is enabled.",
                )
                return
            self.camera_mgr.send_save_config(self.save_dir, self._build_base_name())
            self._push_camera_sync_profile()
            self.camera_mgr.send_sync_enabled(self.camera_sync_check.isChecked())
            self.camera_mgr.send_auto_start(True)
            self.log("CAM", "Auto-start armed for camera test trigger")

        dur_s = self.adv_settings["camera_test_duration_s"]
        try:
            self._test_cam_task = nidaqmx.Task()
            self._test_cam_task.do_channels.add_do_chan(pin)
            self._test_cam_task.write(True)
            self.log("NI", f"Camera trigger HIGH — will hold for {dur_s:.1f} s on {pin}")
            self._test_cam_timer = QTimer(self)
            self._test_cam_timer.setSingleShot(True)
            self._test_cam_timer.timeout.connect(self._end_test_camera)
            self._test_cam_timer.start(int(dur_s * 1000))
        except Exception as exc:
            self._test_cam_task = None
            self.log("NI", f"Camera test failed: {exc}")

    def _end_test_camera(self) -> None:
        if self._test_cam_task is not None:
            try:
                self._test_cam_task.write(False)
                self._test_cam_task.close()
            except Exception:
                pass
            self._test_cam_task = None
            self.log("NI", "Camera trigger LOW (test complete)")

    def _on_test_events(self) -> None:
        events = sorted(self._build_events_list(apply_jitter=True), key=lambda e: e.time_ms)
        if not events:
            self.log("NI", "No events defined to test")
            return
        if self._test_events_running:
            self.log("NI", "Event test already in progress")
            return

        self._test_events_running = True
        t0 = time.perf_counter()
        try:
            for ev in events:
                while True:
                    remain = (ev.time_ms / 1000.0) - (time.perf_counter() - t0)
                    if remain <= 0:
                        break
                    QApplication.processEvents()
                    time.sleep(min(remain, 0.01))
                with nidaqmx.Task() as t_evt:
                    t_evt.do_channels.add_do_chan(ev.pin)
                    t_evt.write(True)
                    time.sleep(max(0.001, ev.pulse_width_ms / 1000.0))
                    t_evt.write(False)
            self.log("NI", f"Test events complete ({len(events)} event(s))")
        except Exception as exc:
            self.log("NI", f"Test events failed: {exc}")
        finally:
            self._test_events_running = False

    # ----------------------- Camera module
    def _on_open_camera(self) -> None:
        if self.camera_mgr.is_running:
            self.log("CAM", "Camera module already running")
            return
        self.camera_mgr.launch(self.save_dir, self._build_base_name())
        self._push_camera_profile()
        self._push_camera_sync_profile()
        self.camera_mgr.send_sync_enabled(self.camera_sync_check.isChecked())
        self.log("CAM", "Camera module launched in separate process")

    def _on_camera_sync_toggled(self, enabled: bool) -> None:
        if self.camera_mgr.is_running:
            self.camera_mgr.send_sync_enabled(bool(enabled))

    def _ensure_camera_adv_defaults(self) -> None:
        self.adv_settings.setdefault("camera_exposure_us", 5000.0)
        self.adv_settings.setdefault("camera_gain_db", 14.0)
        self.adv_settings.setdefault("camera_fps", 60.0)
        self.adv_settings.setdefault("camera_resolution", "1440x1080")
        self.adv_settings.setdefault("camera_codec_profile_index", 0)
        self.adv_settings.setdefault("camera_writer_queue_size", 512)
        self.adv_settings.setdefault("camera_drop_if_full", True)
        self.adv_settings.setdefault("camera_trigger_input_line", "Line0")
        self.adv_settings.setdefault("camera_sync_output_line", "Line1")
        self.adv_settings.setdefault("camera_sync_source_mode", "Frame start active")
        self.adv_settings.setdefault("camera_sync_pulse_us", 1000.0)
        self.adv_settings.setdefault("camera_grab_strategy", 2)
        self.adv_settings.setdefault("camera_image_node_num", 32)
        self.adv_settings.setdefault("camera_output_queue_size", 8)

    def _push_camera_sync_profile(self) -> None:
        self._ensure_camera_adv_defaults()
        self.camera_mgr.send_sync_profile(
            str(self.adv_settings.get("camera_sync_output_line", "Line1")),
            str(self.adv_settings.get("camera_sync_source_mode", "Frame start active")),
            float(self.adv_settings.get("camera_sync_pulse_us", 1000.0)),
        )

    def _push_camera_profile(self) -> None:
        self._ensure_camera_adv_defaults()
        self.camera_mgr.send_camera_profile(
            exposure_us=float(self.adv_settings.get("camera_exposure_us", 5000.0)),
            gain_db=float(self.adv_settings.get("camera_gain_db", 14.0)),
            fps=float(self.adv_settings.get("camera_fps", 60.0)),
            resolution=str(self.adv_settings.get("camera_resolution", "1440x1080")),
        )
        self.camera_mgr.send_recording_profile(
            codec_index=int(self.adv_settings.get("camera_codec_profile_index", 0)),
            queue_size=int(self.adv_settings.get("camera_writer_queue_size", 512)),
            drop_if_full=bool(self.adv_settings.get("camera_drop_if_full", True)),
        )
        self.camera_mgr.send_trigger_profile(
            str(self.adv_settings.get("camera_trigger_input_line", "Line0"))
        )
        self.camera_mgr.send_buffer_profile(
            grab_strategy=int(self.adv_settings.get("camera_grab_strategy", 2)),
            image_node_num=int(self.adv_settings.get("camera_image_node_num", 32)),
            output_queue_size=int(self.adv_settings.get("camera_output_queue_size", 8)),
        )

    def _start_idle_speed_monitor(self) -> None:
        self._idle_speed_err_logged = False
        self._ensure_idle_encoder_task()
        if self._idle_speed_timer is None:
            self._idle_speed_timer = QTimer(self)
            self._idle_speed_timer.setInterval(200)
            self._idle_speed_timer.timeout.connect(self._poll_idle_speed)
        if not self._idle_speed_timer.isActive():
            self._idle_speed_timer.start()

    def _stop_idle_speed_monitor(self) -> None:
        if self._idle_speed_timer is not None:
            self._idle_speed_timer.stop()
        self._close_idle_encoder_task()
        self._idle_speed_last_ticks = None
        self._idle_speed_last_ts = None
        self._idle_speed_start_ticks = None
        self._idle_speed_history.clear()

    def _ensure_idle_encoder_task(self) -> None:
        if self._idle_encoder_task is not None:
            return
        try:
            dev = self.adv_settings["device_name"]
            counter = f"{dev}/ctr2"
            a_pfi = self._io_pin("Encoder_A") or f"/{dev}/PFI4"
            b_pfi = self._io_pin("Encoder_B") or f"/{dev}/PFI5"
            task = nidaqmx.Task()
            encoder_channel = task.ci_channels.add_ci_ang_encoder_chan(
                counter=counter,
                decoding_type=EncoderType.X_4,
                units=AngleUnits.TICKS,
                pulses_per_rev=int(self.adv_settings["encoder_ppr"]),
                initial_angle=0.0,
            )
            encoder_channel.ci_encoder_a_input_term = a_pfi
            encoder_channel.ci_encoder_b_input_term = b_pfi
            task.start()
            self._idle_encoder_task = task
        except Exception as exc:
            if not getattr(self, "_idle_enc_err_logged", False):
                self.log("NI", f"Idle encoder task failed: {exc}")
                self._idle_enc_err_logged = True
            self._idle_encoder_task = None

    def _close_idle_encoder_task(self) -> None:
        if self._idle_encoder_task is not None:
            try:
                self._idle_encoder_task.close()
            except Exception:
                pass
            self._idle_encoder_task = None

    def _read_encoder_ticks_once(self) -> Optional[int]:
        self._ensure_idle_encoder_task()
        if self._idle_encoder_task is None:
            return None
        raw_ticks = self._idle_encoder_task.read()
        return int(round(float(raw_ticks)))

    def _poll_idle_speed(self) -> None:
        if self.worker is not None:
            return
        try:
            now = time.perf_counter()
            ticks = self._read_encoder_ticks_once()
            if ticks is None:
                return
            if self._idle_speed_last_ticks is None or self._idle_speed_last_ts is None:
                self._idle_speed_last_ticks = ticks
                self._idle_speed_last_ts = now
                self._idle_speed_start_ticks = ticks
                self._idle_speed_history.clear()
                return
            dt = now - self._idle_speed_last_ts
            if dt <= 0:
                return
            cm_per_tick = (float(self.adv_settings["wheel_diameter_cm"]) * math.pi) / (
                int(self.adv_settings["encoder_ppr"]) * 4
            )
            start_ticks = self._idle_speed_start_ticks if self._idle_speed_start_ticks is not None else ticks
            dist_cm = (ticks - start_ticks) * cm_per_tick
            self._idle_speed_history.append((now, dist_cm))
            window_s = float(max(0.001, self.adv_settings.get("smoothing_window_s", 0.05)))
            while len(self._idle_speed_history) > 1 and (now - self._idle_speed_history[0][0]) > window_s:
                self._idle_speed_history.popleft()
            old_t, old_d = self._idle_speed_history[0]
            delta_t = now - old_t
            delta_d = dist_cm - old_d
            speed = (delta_d / delta_t) if delta_t > 0 else 0.0
            self.realtime_speed_label.setText(f"{speed:.4f} cm/s")
            self._idle_speed_last_ticks = ticks
            self._idle_speed_last_ts = now
        except Exception as exc:
            if not getattr(self, "_idle_speed_err_logged", False):
                self.log("NI", f"Idle speed error: {exc}")
                self._idle_speed_err_logged = True
            self.realtime_speed_label.setText("-- cm/s")
            self._idle_speed_last_ticks = None
            self._idle_speed_last_ts = None
            self._idle_speed_start_ticks = None
            self._idle_speed_history.clear()

    def _poll_camera_logs(self) -> None:
        for line in self.camera_mgr.drain_logs():
            text = str(line)
            prefix = "STATE_CAMERA_SETTINGS "
            if text.startswith(prefix):
                try:
                    payload = json.loads(text[len(prefix):])
                    if isinstance(payload, dict):
                        for key in (
                            "camera_exposure_us",
                            "camera_gain_db",
                            "camera_fps",
                            "camera_resolution",
                            "camera_codec_profile_index",
                            "camera_writer_queue_size",
                            "camera_drop_if_full",
                            "camera_trigger_input_line",
                            "camera_sync_output_line",
                            "camera_sync_source_mode",
                            "camera_sync_pulse_us",
                            "camera_grab_strategy",
                            "camera_image_node_num",
                            "camera_output_queue_size",
                        ):
                            if key in payload:
                                self.adv_settings[key] = payload[key]
                except Exception:
                    self.log("CAM", f"Invalid camera state payload: {text}")
                continue

            self.log("CAM", text)
            if "Camera connected and grabbing" in text:
                self._push_camera_profile()
                self._push_camera_sync_profile()

    # ----------------------- Advanced settings
    def _on_advanced(self) -> None:
        old_dev = self.adv_settings.get("device_name", "Dev1")
        timebase_options = self._get_timebase_options(old_dev)
        dlg = AdvancedSettingsDialog(dict(self.adv_settings), timebase_options=timebase_options, parent=self)
        if dlg.exec():
            self.adv_settings = dlg.apply_to_settings()
            self.adv_settings["timebase_freq_hz"] = self._timebase_freq_guess(
                self.adv_settings.get("timebase_terminal", "20MHzTimebase")
            )
            new_dev = self.adv_settings.get("device_name", "Dev1")
            if new_dev != old_dev:
                self._update_io_table_device(new_dev)
                self.log("GUI", f"IO table pins updated: {old_dev} → {new_dev}")
                self._stop_idle_speed_monitor()
                self._start_idle_speed_monitor()
                # Device changed -> refresh selectable timebase defaults
                tb_opts = self._get_timebase_options(new_dev)
                if tb_opts:
                    self.adv_settings["timebase_terminal"] = tb_opts[0]
                    self.adv_settings["timebase_freq_hz"] = self._timebase_freq_guess(tb_opts[0])
            self.log("GUI", "Advanced settings updated")

    # ----------------------- Save / Load Config (JSON)
    def _on_save_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Config", self.save_dir, "JSON Config (*.json)"
        )
        if not path:
            return
        self._ensure_camera_adv_defaults()
        data = {
            "config_version": 2,
            "io_lines": [],
            "events": [],
            "adv_settings": dict(self.adv_settings),
            "ui_state": {
                "save_dir": os.path.abspath(self.save_dir),
                "animal_id": self.animal_id_edit.text().strip(),
                "suffix": self.suffix_edit.text().strip(),
                "enable_microscope_sync": bool(self.microscope_sync_check.isChecked()),
                "enable_camera_sync": bool(self.camera_sync_check.isChecked()),
                "auto_start_camera": bool(self.auto_start_camera_check.isChecked()),
            },
        }
        for row in range(self.io_table.rowCount()):
            pin = self.io_table.item(row, 0).text() if self.io_table.item(row, 0) else ""
            direction = self.io_table.item(row, 1).text() if self.io_table.item(row, 1) else ""
            function = self.io_table.item(row, 2).text() if self.io_table.item(row, 2) else ""
            data["io_lines"].append({"pin": pin, "direction": direction, "function": function})
        for row in range(self.evt_table.rowCount()):
            try:
                time_ms = float(self.evt_table.item(row, 0).text())
                pin = self.evt_table.item(row, 1).text().strip()
                pw_ms = float(self.evt_table.item(row, 2).text())
                jitter_item = self.evt_table.item(row, 3)
                jitter_enabled = bool(
                    jitter_item
                    and jitter_item.checkState() == Qt.CheckState.Checked
                )
                remark = self.evt_table.item(row, 4).text().strip() if self.evt_table.item(row, 4) else ""
            except (ValueError, AttributeError):
                continue
            data["events"].append(
                {
                    "time_ms": time_ms,
                    "pin": pin,
                    "pulse_width_ms": pw_ms,
                    "jitter": jitter_enabled,
                    "remark": remark,
                }
            )
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            self.log("GUI", f"Config saved: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save error", str(exc))

    def _on_load_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Config", self.save_dir, "JSON Config (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as exc:
            QMessageBox.critical(self, "Load error", str(exc))
            return

        # Restore IO table
        if "io_lines" in data:
            self.io_table.setRowCount(0)
            for entry in data["io_lines"]:
                row = self.io_table.rowCount()
                self.io_table.insertRow(row)
                pin_item = QTableWidgetItem(entry.get("pin", ""))
                dir_item = QTableWidgetItem(entry.get("direction", ""))
                dir_item.setFlags(dir_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                func_item = QTableWidgetItem(entry.get("function", ""))
                func_item.setFlags(func_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.io_table.setItem(row, 0, pin_item)
                self.io_table.setItem(row, 1, dir_item)
                self.io_table.setItem(row, 2, func_item)
            self._sort_io_table_by_pin()

        # Restore events table
        if "events" in data:
            self.evt_table.setRowCount(0)
            for entry in data["events"]:
                self._add_event_row(
                    entry.get("time_ms", 0.0),
                    entry.get("pin", ""),
                    entry.get("pulse_width_ms", 5.0),
                    entry.get("remark", ""),
                    bool(entry.get("jitter", False)),
                )

        # Restore advanced settings
        if "adv_settings" in data:
            for k, v in data["adv_settings"].items():
                self.adv_settings[k] = v
        self._ensure_camera_adv_defaults()
        self.adv_settings.setdefault("timebase_terminal", "20MHzTimebase")
        self.adv_settings.setdefault("jitter_min_ms", -1000.0)
        self.adv_settings.setdefault("jitter_max_ms", 1000.0)
        self.adv_settings.setdefault(
            "timebase_freq_hz",
            self._timebase_freq_guess(self.adv_settings.get("timebase_terminal", "20MHzTimebase")),
        )

        # Restore UI/session state.
        ui_state = data.get("ui_state", {}) if isinstance(data, dict) else {}
        saved_dir = str(ui_state.get("save_dir", "")).strip()
        if saved_dir:
            self.save_dir = os.path.abspath(saved_dir)
            self.path_edit.setText(self.save_dir)

        self.animal_id_edit.setText(str(ui_state.get("animal_id", self.animal_id_edit.text())).strip())
        self.suffix_edit.setText(str(ui_state.get("suffix", self.suffix_edit.text())).strip())
        self.microscope_sync_check.setChecked(bool(ui_state.get("enable_microscope_sync", self.microscope_sync_check.isChecked())))
        self.camera_sync_check.setChecked(bool(ui_state.get("enable_camera_sync", self.camera_sync_check.isChecked())))
        self.auto_start_camera_check.setChecked(bool(ui_state.get("auto_start_camera", self.auto_start_camera_check.isChecked())))

        if self.camera_mgr.is_running:
            self.camera_mgr.send_save_config(self.save_dir, self._build_base_name())
            self._push_camera_profile()
            self._push_camera_sync_profile()
            self.camera_mgr.send_sync_enabled(self.camera_sync_check.isChecked())

        self.log("GUI", f"Config loaded: {path}")

    # ----------------------- View / Load
    def _on_view_latest(self) -> None:
        target = self.last_saved_path
        if not target or not os.path.exists(target):
            target = self._find_most_recent_csv()
        if not target:
            QMessageBox.information(
                self, "View", "No recent CSV found in the save folder."
            )
            return
        self._show_plot(target)

    def _on_load_session(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load session CSV", self.save_dir, "CSV Files (*.csv)"
        )
        if not path:
            return
        self._show_plot(path)

    def _show_plot(self, csv_path: str) -> None:
        dlg = SessionPlotDialog(self)
        try:
            dlg.plot_csv(csv_path)
            self.log("GUI", f"Plotting: {csv_path}")
        except Exception as exc:
            QMessageBox.critical(self, "Plot error", str(exc))
            self.log("GUI", f"Plot error: {exc}")
            dlg.close()
            return
        dlg.exec()

    def _find_most_recent_csv(self) -> Optional[str]:
        if not os.path.isdir(self.save_dir):
            return None
        csv_files = [
            os.path.join(self.save_dir, f)
            for f in os.listdir(self.save_dir)
            if f.lower().endswith(".csv")
        ]
        if not csv_files:
            return None
        return max(csv_files, key=os.path.getmtime)

    # ----------------------- Cleanup
    def closeEvent(self, event) -> None:
        # Stop session if running
        if self.worker is not None:
            self.worker.request_stop()
        self._cleanup_worker()
        self._stop_idle_speed_monitor()
        # Stop camera test if in progress
        self._end_test_camera()
        # Quit camera process
        self.camera_mgr.quit()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    multiprocessing.freeze_support()
    multiprocessing.set_start_method("spawn", force=True)
    app = QApplication(sys.argv)
    window = BehaviorHubWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
