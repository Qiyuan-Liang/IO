"""BehaviorHub GUI — Master experiment controller.

Replicates the NI_V1.4_camera.py session logic with a PyQt6 UI,
coordinates camera recording via a separate process, and provides
velocity/location visualization.

to install as exe: pyinstaller --noconfirm BehaviorHub_easy.spec
"""

import csv
import math
import multiprocessing
import os
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
    Signal,
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


@dataclass
class SessionConfig:
    """Everything the NI worker needs to run one recording session."""
    save_path: str

    # NI device
    device_name: str = "Dev1"

    # Pin assignments (defaults from NI_V1.4_camera.py)
    frame_clock_pin: str = "/Dev1/PFI0"
    arduino_trig_pin: str = "/Dev1/PFI1"
    microscope_start_line: str = "Dev1/port0/line2"
    arduino_input_line: str = "Dev1/port0/line3"
    encoder_a_pfi: str = "/Dev1/PFI4"
    encoder_b_pfi: str = "/Dev1/PFI5"
    camera_sync_line: str = "Dev1/port0/line14"
    camera_sync_counter_pin: str = "/Dev1/PFI14"
    camera_trigger_pin: str = "Dev1/port0/line15"

    # Counters
    time_counter: str = "Dev1/ctr0"
    camera_sync_counter: str = "Dev1/ctr1"
    encoder_counter: str = "Dev1/ctr2"

    # Physical
    internal_timebase: str = "/Dev1/100MHzTimebase"
    timebase_freq: float = 100_000_000.0
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

        # Gather DI channels from the IO table (input direction only)
        di_channels = [ch for ch in cfg.input_channels if ch.direction == "Input"]
        if not di_channels:
            raise RuntimeError("No input channels configured in IO table.")
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

        use_counter_sync = cfg.enable_camera_sync

        with nidaqmx.Task() as relay, \
             nidaqmx.Task() as logger_data, \
             nidaqmx.Task() as logger_time, \
             nidaqmx.Task() as logger_enc, \
             nidaqmx.Task() as logger_camcnt, \
             nidaqmx.Task() as camera_trigger_task:

            # 1. RELAY — forward first frame clock edge to Arduino
            relay.ai_channels.add_ai_voltage_chan(f"{cfg.device_name}/ai0")
            relay.timing.cfg_samp_clk_timing(
                rate=1000, sample_mode=AcquisitionType.CONTINUOUS
            )
            relay.triggers.start_trigger.cfg_dig_edge_start_trig(
                cfg.frame_clock_pin, Edge.FALLING
            )
            relay.export_signals.export_signal(
                Signal.START_TRIGGER, cfg.arduino_trig_pin
            )

            # 2. DATA LOGGER — DI channels clocked by microscope frame clock
            logger_data.di_channels.add_di_chan(
                di_csv, line_grouping=LineGrouping.CHAN_PER_LINE
            )
            logger_data.timing.cfg_samp_clk_timing(
                rate=cfg.estimated_fps,
                source=cfg.frame_clock_pin,
                active_edge=Edge.FALLING,
                sample_mode=AcquisitionType.CONTINUOUS,
            )

            # 3. TIME LOGGER — 100 MHz counter for precise timestamps
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

            # 4. ENCODER LOGGER — X4 quadrature on PFI4/PFI5
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

            # 5. CAMERA SYNC COUNTER (optional)
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
                    except Exception as exc:
                        use_counter_sync = False
                        self.status.emit(
                            f"Camera sync counter setup failed ({exc}); falling back to sampled DI."
                        )

            # 6. CAMERA TRIGGER GATE — level output on camera trigger pin
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
                logger_data.start()
                if use_counter_sync:
                    logger_camcnt.start()
                relay.start()

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

                prev_camera_sync = None
                prev_arduino_state = None
                arduino_ch_idx = None
                cam_sync_idx = None
                for idx_ch, ch in enumerate(di_channels):
                    if ch.function == "Arduino_State":
                        arduino_ch_idx = idx_ch
                    elif ch.function == "Camera_Frame":
                        cam_sync_idx = idx_ch

                try:
                    while not self._stop_event.is_set():
                        samples_available = logger_data.in_stream.avail_samp_per_chan

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

                        chunk_data = logger_data.read(
                            number_of_samples_per_channel=samples_available
                        )
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

                        if len(di_channels) == 1:
                            di_by_channel = [chunk_data]
                        elif (
                            len(chunk_data) >= 2
                            and isinstance(chunk_data[0], list)
                        ):
                            di_by_channel = chunk_data
                        else:
                            di_by_channel = [chunk_data]

                        # Camera sync counter (hardware, for diagnostics only)
                        if use_counter_sync:
                            try:
                                hw_cam_count = int(logger_camcnt.read())
                            except Exception:
                                pass

                        sample_count = min(
                            len(chunk_time),
                            len(chunk_enc),
                            *[len(ch_data) for ch_data in di_by_channel],
                        )

                        rows = []
                        for i in range(sample_count):
                            curr_ticks_time = chunk_time[i]

                            if start_ticks_time is None:
                                start_ticks_time = curr_ticks_time
                                start_ticks_enc = parse_signed_32bit(chunk_enc[i])

                            exact_time_s = (
                                (curr_ticks_time - start_ticks_time)
                                / cfg.timebase_freq
                            )
                            total_frames += 1

                            # DI values for each input channel
                            input_values = [
                                int(
                                    bool(di_by_channel[ch_idx][i])
                                    if ch_idx < len(di_by_channel)
                                    else 0
                                )
                                for ch_idx in range(len(di_channels))
                            ]

                            # Detect Arduino event onsets (rising edge)
                            if arduino_ch_idx is not None:
                                ard_val = input_values[arduino_ch_idx]
                                if prev_arduino_state is not None and prev_arduino_state == 0 and ard_val == 1:
                                    arduino_onsets.append(exact_time_s)
                                prev_arduino_state = ard_val

                            # Camera frame count from shared counter (actual video frames)
                            if self._shared_cam_counter is not None:
                                camera_frame_count = self._shared_cam_counter.value

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

                f.flush()

            # Deassert camera trigger
            self.status.emit("Deasserting camera trigger (LOW)...")
            camera_trigger_task.write(False)

        if self._stop_event.is_set() and stopped_by == "Abort":
            stopped_by = "Aborted by user"

        session_wall_s = time.time() - session_wall_start
        if total_frames > 0 and start_ticks_time is not None:
            scope_fps = total_frames / session_wall_s if session_wall_s > 0 else 0.0
        else:
            scope_fps = 0.0

        summary_parts = [
            f"Done. Scope frames: {total_frames}",
            f"Camera frames: {camera_frame_count}",
            f"Duration: {session_wall_s:.2f}s",
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
    def __init__(self, settings: dict, parent=None):
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

        self.camera_sync_edge_combo = QLineEdit(
            settings.get("camera_sync_active_edge", "falling")
        )

        form.addRow("NI Device", self.device_edit)
        form.addRow("Microscope pulse width", self.microscope_pulse_spin)
        form.addRow("Camera test trigger dur", self.camera_test_dur_spin)
        form.addRow("Wheel diameter", self.wheel_diam_spin)
        form.addRow("Encoder PPR", self.ppr_spin)
        form.addRow("Velocity smoothing window", self.smoothing_spin)
        form.addRow("Silence timeout", self.silence_spin)
        form.addRow("Estimated sync Hz", self.estimated_fps_spin)
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
        self.settings["wheel_diameter_cm"] = float(self.wheel_diam_spin.value())
        self.settings["encoder_ppr"] = int(self.ppr_spin.value())
        self.settings["smoothing_window_s"] = float(self.smoothing_spin.value())
        self.settings["silence_timeout_s"] = float(self.silence_spin.value())
        self.settings["estimated_fps"] = float(self.estimated_fps_spin.value())
        self.settings["camera_sync_active_edge"] = (
            self.camera_sync_edge_combo.text().strip() or "falling"
        )
        return self.settings


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
        self.resize(600, 700)

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
            "wheel_diameter_cm": 15.5,
            "encoder_ppr": 1024,
            "smoothing_window_s": 0.05,
            "silence_timeout_s": 0.3,
            "estimated_fps": 7000.0,
            "camera_sync_active_edge": "falling",
        }

        # Test trigger state
        self._test_cam_task = None
        self._test_cam_timer: Optional[QTimer] = None
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
        self.abort_btn = QPushButton("Abort")
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
        exp_layout.addWidget(self.animal_id_edit, 1, 1, 1, 3)
        exp_layout.addWidget(QLabel("Suffix"), 2, 0)
        exp_layout.addWidget(self.suffix_edit, 2, 1, 1, 3)
        exp_layout.addWidget(self.frame_state_label, 3, 0, 1, 4)
        exp_layout.addWidget(QLabel("Live speed"), 4, 0)
        exp_layout.addWidget(self.realtime_speed_label, 4, 1, 1, 3)
        exp_layout.addWidget(self.start_btn, 5, 0, 1, 4)
        exp_layout.addWidget(self.abort_btn, 5, 0, 1, 4)

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
        self.test_arduino_btn = QPushButton("Test Arduino trigger")

        hw_layout.addWidget(self.microscope_sync_check)
        hw_layout.addWidget(self.test_microscope_btn)
        hw_layout.addSpacing(6)
        hw_layout.addWidget(self.camera_sync_check)
        hw_layout.addWidget(self.auto_start_camera_check)
        hw_layout.addWidget(self.test_camera_btn)
        hw_layout.addWidget(self.test_arduino_btn)
        hw_layout.addStretch(1)

        # —— Column 3: Software ——
        sw_group = QGroupBox("Software")
        sw_layout = QVBoxLayout(sw_group)

        self.camera_module_btn = QPushButton("Camera Module")
        self.advanced_btn = QPushButton("Advanced Settings")
        self.view_latest_btn = QPushButton("View Latest")
        self.load_session_btn = QPushButton("Load Session")

        sw_layout.addWidget(self.camera_module_btn)
        sw_layout.addWidget(self.advanced_btn)
        sw_layout.addSpacing(12)
        sw_layout.addWidget(self.view_latest_btn)
        sw_layout.addWidget(self.load_session_btn)
        sw_layout.addStretch(1)

        row1.addWidget(exp_group, 1)
        row1.addWidget(hw_group, 1)
        row1.addWidget(sw_group, 1)

        # ============ ROW 2 — IO lines table ============
        io_group = QGroupBox("IO Lines")
        io_layout = QVBoxLayout(io_group)

        self.io_table = QTableWidget(0, 3)
        self.io_table.setHorizontalHeaderLabels(["Pin", "Direction", "Function"])
        self.io_table.horizontalHeader().setStretchLastSection(True)
        self.io_table.setMinimumHeight(150)
        io_layout.addWidget(self.io_table)

        self._populate_default_io_table()

        # ============ ROW 3 — Log area ============
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(120)

        root_layout.addLayout(row1)
        root_layout.addWidget(io_group)
        root_layout.addWidget(self.log_box)

    def _populate_default_io_table(self) -> None:
        """Fill IO table with NI_V1.4_camera.py defaults."""
        defaults = [
            ("/Dev1/PFI0", "Input", "Frame_Clock"),
            ("/Dev1/PFI1", "Output", "Arduino_Trigger"),
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

    def _bind_signals(self) -> None:
        self.browse_btn.clicked.connect(self._on_browse)
        self.start_btn.clicked.connect(self._on_start)
        self.abort_btn.clicked.connect(self._on_abort)
        self.test_microscope_btn.clicked.connect(self._on_test_microscope)
        self.test_camera_btn.clicked.connect(self._on_test_camera)
        self.test_arduino_btn.clicked.connect(self._on_test_arduino)
        self.camera_module_btn.clicked.connect(self._on_open_camera)
        self.advanced_btn.clicked.connect(self._on_advanced)
        self.view_latest_btn.clicked.connect(self._on_view_latest)
        self.load_session_btn.clicked.connect(self._on_load_session)
        self.camera_sync_check.toggled.connect(self._on_camera_sync_toggled)

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
        channels = []
        for row in range(self.io_table.rowCount()):
            dir_item = self.io_table.item(row, 1)
            if dir_item is None or dir_item.text() != "Input":
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
            channels.append(IOLine(pin=pin, direction="Input", function=func))
        return channels

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
            arduino_trig_pin=self._io_pin("Arduino_Trigger") or f"/{dev}/PFI1",
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
            time_counter=f"{dev}/ctr0",
            camera_sync_counter=f"{dev}/ctr1",
            encoder_counter=f"{dev}/ctr2",
            internal_timebase=f"/{dev}/100MHzTimebase",
            timebase_freq=100_000_000.0,
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
        try:
            cfg = self._build_session_config()
        except Exception as exc:
            QMessageBox.critical(self, "Config error", str(exc))
            self.log("GUI", f"Config error: {exc}")
            return

        self.last_saved_path = cfg.save_path
        self.log("GUI", f"Output: {os.path.basename(cfg.save_path)}")
        self._stop_idle_speed_monitor()

        # Camera auto-start
        if self.auto_start_camera_check.isChecked():
            if not self.camera_mgr.is_running:
                self.camera_mgr.launch(self.save_dir, self._build_base_name())
                self.log("CAM", "Camera module auto-launched for recording")
            self.camera_mgr.send_save_config(
                self.save_dir, self._build_base_name()
            )
            self.camera_mgr.send_auto_start(True)
            self.log("CAM", "Auto-start enabled via master")
        elif self.camera_mgr.is_running:
            # Camera module open but auto-start off: directly start recording
            self.camera_mgr.send_save_config(
                self.save_dir, self._build_base_name()
            )
            self.camera_mgr.send_start_recording()
            self.log("CAM", "Recording started via master")

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

    def _on_test_arduino(self) -> None:
        pin = self._io_pin("Arduino_Trigger")
        if not pin:
            self.log("NI", "Arduino_Trigger pin not configured")
            return
        try:
            with nidaqmx.Task() as t:
                t.do_channels.add_do_chan(pin)
                t.write(False)
                t.write(True)
                time.sleep(0.01)
                t.write(False)
            self.log("NI", f"Arduino trigger pulse sent on {pin}")
        except Exception as exc:
            self.log("NI", f"Arduino test failed: {exc}")

    # ----------------------- Camera module
    def _on_open_camera(self) -> None:
        if self.camera_mgr.is_running:
            self.log("CAM", "Camera module already running")
            return
        self.camera_mgr.launch(self.save_dir, self._build_base_name())
        self._push_camera_sync_profile()
        self.camera_mgr.send_sync_enabled(self.camera_sync_check.isChecked())
        self.log("CAM", "Camera module launched in separate process")

    def _on_camera_sync_toggled(self, enabled: bool) -> None:
        if self.camera_mgr.is_running:
            self.camera_mgr.send_sync_enabled(bool(enabled))

    def _push_camera_sync_profile(self) -> None:
        self.camera_mgr.send_sync_profile("Line1", "Frame start active", 1000.0)

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
            self.log("CAM", str(line))
            text = str(line)
            if "Camera connected and grabbing" in text:
                self._push_camera_sync_profile()

    # ----------------------- Advanced settings
    def _on_advanced(self) -> None:
        old_dev = self.adv_settings.get("device_name", "Dev1")
        dlg = AdvancedSettingsDialog(dict(self.adv_settings), self)
        if dlg.exec():
            self.adv_settings = dlg.apply_to_settings()
            new_dev = self.adv_settings.get("device_name", "Dev1")
            if new_dev != old_dev:
                self._update_io_table_device(new_dev)
                self.log("GUI", f"IO table pins updated: {old_dev} → {new_dev}")
                self._stop_idle_speed_monitor()
                self._start_idle_speed_monitor()
            self.log("GUI", "Advanced settings updated")

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
