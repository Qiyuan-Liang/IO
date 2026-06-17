import csv
import math
import os
import sys
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
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
    QSpinBox,
    QDoubleSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
    QComboBox,
    QTextEdit,
    QSizePolicy,
)

import nidaqmx
from nidaqmx.constants import (
    AcquisitionType,
    AngleUnits,
    CountDirection,
    Edge,
    EncoderType,
    Level,
    LineGrouping,
    Signal,
    TimeUnits,
)
from nidaqmx.system import System


def parse_signed_32bit(number):
    number = int(number)
    if number >= (1 << 31):
        number -= (1 << 32)
    return number


def normalize_terminal(device_name, terminal_text):
    terminal = terminal_text.strip()
    if terminal.startswith("/"):
        return terminal
    if terminal.startswith(device_name + "/"):
        return f"/{terminal}"
    if terminal.startswith("PFI"):
        return f"/{device_name}/{terminal}"
    return terminal


@dataclass
class OutputEvent:
    channel: str
    start_ms: int
    duration_ms: int


@dataclass
class InputChannel:
    channel: str
    name: str
    rate_mode: str = "sync"
    fixed_rate_hz: float = 10000.0


@dataclass
class RecorderConfig:
    save_path: str
    device_name: str = "Dev1"
    frame_clock_pin: str = "/Dev1/PFI0"
    microscope_start_line: str = "Dev1/port0/line2"
    arduino_trigger_pin: str = "/Dev1/PFI1"
    arduino_input_default: str = "Dev1/port0/line3"
    encoder_counter: str = "Dev1/ctr2"
    time_counter: str = "Dev1/ctr0"
    encoder_a_pfi: str = "/Dev1/PFI4"
    encoder_b_pfi: str = "/Dev1/PFI5"
    internal_timebase: str = "/Dev1/100MHzTimebase"
    timebase_freq: float = 100000000.0
    estimated_fps: float = 10000.0
    wheel_diameter_cm: float = 15.5
    encoder_ppr: int = 1024
    smoothing_window_s: float = 0.05
    silence_timeout_s: float = 0.3
    stop_with_microscope: bool = True
    expected_duration_ms: int = 5000
    output_events: list[OutputEvent] = field(default_factory=list)
    input_channels: list[InputChannel] = field(default_factory=list)
    output_counters: list[str] = field(default_factory=lambda: ["Dev1/ctr1", "Dev1/ctr3"])


class SettingsDialog(QDialog):
    def __init__(self, config: RecorderConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advanced Settings")
        self.config = config

        form = QFormLayout(self)

        self.device_edit = QLineEdit(config.device_name)
        self.frame_clock_edit = QLineEdit(config.frame_clock_pin)
        self.micro_line_edit = QLineEdit(config.microscope_start_line)
        self.arduino_trig_edit = QLineEdit(config.arduino_trigger_pin)
        self.time_counter_edit = QLineEdit(config.time_counter)
        self.encoder_counter_edit = QLineEdit(config.encoder_counter)
        self.encoder_a_edit = QLineEdit(config.encoder_a_pfi)
        self.encoder_b_edit = QLineEdit(config.encoder_b_pfi)
        self.timebase_edit = QLineEdit(config.internal_timebase)
        self.output_counter_edit = QLineEdit(",".join(config.output_counters))

        self.estimated_fps_spin = QDoubleSpinBox()
        self.estimated_fps_spin.setRange(1.0, 200000.0)
        self.estimated_fps_spin.setDecimals(1)
        self.estimated_fps_spin.setValue(config.estimated_fps)

        self.timebase_freq_spin = QDoubleSpinBox()
        self.timebase_freq_spin.setRange(1.0, 1e9)
        self.timebase_freq_spin.setDecimals(1)
        self.timebase_freq_spin.setValue(config.timebase_freq)

        self.wheel_diam_spin = QDoubleSpinBox()
        self.wheel_diam_spin.setRange(0.1, 1000.0)
        self.wheel_diam_spin.setDecimals(3)
        self.wheel_diam_spin.setValue(config.wheel_diameter_cm)

        self.ppr_spin = QSpinBox()
        self.ppr_spin.setRange(1, 100000)
        self.ppr_spin.setValue(config.encoder_ppr)

        self.smoothing_spin = QDoubleSpinBox()
        self.smoothing_spin.setRange(0.001, 2.0)
        self.smoothing_spin.setDecimals(3)
        self.smoothing_spin.setValue(config.smoothing_window_s)

        self.silence_spin = QDoubleSpinBox()
        self.silence_spin.setRange(0.01, 10.0)
        self.silence_spin.setDecimals(3)
        self.silence_spin.setValue(config.silence_timeout_s)

        form.addRow("Device", self.device_edit)
        form.addRow("Frame clock pin", self.frame_clock_edit)
        form.addRow("Microscope start line", self.micro_line_edit)
        form.addRow("Arduino trigger pin", self.arduino_trig_edit)
        form.addRow("Time counter", self.time_counter_edit)
        form.addRow("Encoder counter", self.encoder_counter_edit)
        form.addRow("Encoder A PFI", self.encoder_a_edit)
        form.addRow("Encoder B PFI", self.encoder_b_edit)
        form.addRow("Internal timebase", self.timebase_edit)
        form.addRow("Output counters (comma)", self.output_counter_edit)
        form.addRow("Estimated sync Hz", self.estimated_fps_spin)
        form.addRow("Timebase frequency", self.timebase_freq_spin)
        form.addRow("Wheel diameter (cm)", self.wheel_diam_spin)
        form.addRow("Encoder PPR", self.ppr_spin)
        form.addRow("Smoothing window (s)", self.smoothing_spin)
        form.addRow("Silence timeout (s)", self.silence_spin)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("Apply")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        form.addRow(btn_row)

    def apply_to_config(self):
        self.config.device_name = self.device_edit.text().strip() or self.config.device_name
        self.config.frame_clock_pin = self.frame_clock_edit.text().strip() or self.config.frame_clock_pin
        self.config.microscope_start_line = self.micro_line_edit.text().strip() or self.config.microscope_start_line
        self.config.arduino_trigger_pin = self.arduino_trig_edit.text().strip() or self.config.arduino_trigger_pin
        self.config.time_counter = self.time_counter_edit.text().strip() or self.config.time_counter
        self.config.encoder_counter = self.encoder_counter_edit.text().strip() or self.config.encoder_counter
        self.config.encoder_a_pfi = self.encoder_a_edit.text().strip() or self.config.encoder_a_pfi
        self.config.encoder_b_pfi = self.encoder_b_edit.text().strip() or self.config.encoder_b_pfi
        self.config.internal_timebase = self.timebase_edit.text().strip() or self.config.internal_timebase
        self.config.output_counters = [item.strip() for item in self.output_counter_edit.text().split(",") if item.strip()]
        self.config.estimated_fps = float(self.estimated_fps_spin.value())
        self.config.timebase_freq = float(self.timebase_freq_spin.value())
        self.config.wheel_diameter_cm = float(self.wheel_diam_spin.value())
        self.config.encoder_ppr = int(self.ppr_spin.value())
        self.config.smoothing_window_s = float(self.smoothing_spin.value())
        self.config.silence_timeout_s = float(self.silence_spin.value())


class LogViewerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Log Viewer")
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

    @staticmethod
    def _extract_segments(time_values, marker_values):
        segments = []
        active_start = None
        last_time = None
        for current_time, marker in zip(time_values, marker_values):
            if marker and active_start is None:
                active_start = current_time
            if (not marker) and active_start is not None:
                segments.append((active_start, last_time if last_time is not None else current_time))
                active_start = None
            last_time = current_time
        if active_start is not None and last_time is not None:
            segments.append((active_start, last_time))
        return segments

    def plot_csv(self, file_path):
        with open(file_path, "r", newline="") as file_obj:
            reader = csv.DictReader(file_obj)
            fieldnames = reader.fieldnames or []
            rows = list(reader)

        if not rows:
            raise RuntimeError("Selected CSV is empty.")

        if "Time_s" not in fieldnames:
            raise RuntimeError("CSV must contain 'Time_s' column.")

        time_values = []
        position_values = []
        velocity_values = []
        aligned_rows = []

        for row in rows:
            time_val = self._safe_float(row.get("Time_s", ""))
            if time_val is None:
                continue
            time_values.append(time_val)
            position_values.append(self._safe_float(row.get("Zeroed_Dist_cm", "")))
            velocity_values.append(self._safe_float(row.get("Smoothed_Vel_cm_s", "")))
            aligned_rows.append(row)

        if not time_values:
            raise RuntimeError("No valid numeric Time_s data found.")

        reserved_cols = {"Frame_ID", "Time_s", "Raw_Ticks_Signed", "Zeroed_Dist_cm", "Smoothed_Vel_cm_s"}

        def is_binary_column(column_name):
            non_empty = 0
            for row in aligned_rows:
                value = row.get(column_name, "")
                if value in ("", None):
                    continue
                numeric = self._safe_float(value)
                if numeric is None:
                    return False
                if numeric not in (0.0, 1.0):
                    return False
                non_empty += 1
            return non_empty > 0

        out_columns = [name for name in fieldnames if name.startswith("Out_") and is_binary_column(name)]
        other_binary_columns = [
            name for name in fieldnames
            if name not in reserved_cols and (not name.startswith("Out_")) and is_binary_column(name)
        ]
        event_columns = out_columns + other_binary_columns

        self.figure.clear()
        ax_pos = self.figure.add_subplot(311)
        ax_vel = self.figure.add_subplot(312, sharex=ax_pos)
        ax_evt = self.figure.add_subplot(313, sharex=ax_pos)

        pos_plot_values = [value if value is not None else float("nan") for value in position_values]
        vel_plot_values = [value if value is not None else float("nan") for value in velocity_values]

        ax_pos.plot(time_values, pos_plot_values, linewidth=1.0, color="tab:blue")
        ax_pos.set_ylabel("Position (cm)")
        ax_pos.grid(True, alpha=0.3)

        ax_vel.plot(time_values, vel_plot_values, linewidth=1.0, color="tab:orange")
        ax_vel.set_ylabel("Velocity (cm/s)")
        ax_vel.grid(True, alpha=0.3)

        if event_columns:
            y_ticks = []
            y_labels = []
            for index, event_col in enumerate(event_columns):
                marker_values = [int(self._safe_float(row.get(event_col, 0) or 0) == 1.0) for row in aligned_rows]
                segments = self._extract_segments(time_values, marker_values)
                y_level = index + 1
                for start_time, end_time in segments:
                    ax_evt.hlines(y=y_level, xmin=start_time, xmax=end_time, linewidth=3)
                y_ticks.append(y_level)
                y_labels.append(event_col)
            ax_evt.set_yticks(y_ticks)
            ax_evt.set_yticklabels(y_labels)
        else:
            ax_evt.text(0.5, 0.5, "No Out_* event columns found", ha="center", va="center", transform=ax_evt.transAxes)
            ax_evt.set_yticks([])

        ax_evt.set_xlabel("Time (s)")
        ax_evt.set_ylabel("Events")
        ax_evt.grid(True, axis="x", alpha=0.3)

        self.figure.tight_layout()
        self.canvas.draw()
        self.info_box.setPlainText(
            f"File: {file_path}\nRows: {len(rows)}\n"
            f"Detected event columns: {', '.join(event_columns) if event_columns else 'None'}"
        )


class NIRecorderWorker(QObject):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    status = pyqtSignal(str)
    velocity = pyqtSignal(float)
    frame_count = pyqtSignal(int)

    def __init__(self, config: RecorderConfig):
        super().__init__()
        self.config = config
        self._stop_event = threading.Event()

    def request_stop(self):
        self._stop_event.set()

    def _build_csv_header(self):
        header = ["Frame_ID", "Time_s"]
        header.extend([channel.name for channel in self.config.input_channels])
        for event in self.config.output_events:
            safe = event.channel.replace("/", "_").replace(" ", "")
            header.append(f"Out_{safe}_{event.start_ms}ms_{event.duration_ms}ms")
        header.extend(["Raw_Ticks_Signed", "Zeroed_Dist_cm", "Smoothed_Vel_cm_s"])
        return header

    def _emit_sampling_plan_warning_if_needed(self):
        fixed = [channel for channel in self.config.input_channels if channel.rate_mode == "fixed"]
        if fixed:
            names = ", ".join(channel.name for channel in fixed)
            raise RuntimeError(
                "Fixed-rate input channels are not enabled in this safe mode. "
                "Use Follow Sync for all channels so each CSV row matches microscope frames. "
                f"Channels in fixed mode: {names}."
            )

    def run(self):
        try:
            self._emit_sampling_plan_warning_if_needed()
            self._run_impl()
        except Exception as exception:
            self.error.emit(str(exception))

    def _run_impl(self):
        config = self.config
        cm_per_rev = config.wheel_diameter_cm * math.pi
        ticks_per_rev = config.encoder_ppr * 4
        cm_per_tick = cm_per_rev / ticks_per_rev

        if not config.input_channels:
            raise RuntimeError("At least one input channel is required.")

        self.status.emit("Arming NI tasks...")

        total_frames = 0
        last_data_time = time.time()
        start_ticks_time = None
        start_ticks_enc = None
        history_buffer = deque()
        flush_deadline = time.time() + 1.0
        stopped_by = "Abort"

        pulse_tasks = []
        dynamic_header = self._build_csv_header()
        di_channels_csv = ",".join(channel.channel for channel in config.input_channels)

        with nidaqmx.Task() as relay, \
             nidaqmx.Task() as logger_data, \
             nidaqmx.Task() as logger_time, \
             nidaqmx.Task() as logger_enc:

            relay.ai_channels.add_ai_voltage_chan(f"{config.device_name}/ai0")
            relay.timing.cfg_samp_clk_timing(rate=1000, sample_mode=AcquisitionType.CONTINUOUS)
            relay.triggers.start_trigger.cfg_dig_edge_start_trig(config.frame_clock_pin, Edge.FALLING)
            relay.export_signals.export_signal(Signal.START_TRIGGER, config.arduino_trigger_pin)

            logger_data.di_channels.add_di_chan(di_channels_csv, line_grouping=LineGrouping.CHAN_PER_LINE)
            logger_data.timing.cfg_samp_clk_timing(
                rate=config.estimated_fps,
                source=config.frame_clock_pin,
                active_edge=Edge.FALLING,
                sample_mode=AcquisitionType.CONTINUOUS,
            )
            logger_data.in_stream.input_buf_size = int(max(20000, config.estimated_fps * 3))

            ctr_time = logger_time.ci_channels.add_ci_count_edges_chan(
                counter=config.time_counter,
                edge=Edge.RISING,
                initial_count=0,
                count_direction=CountDirection.COUNT_UP,
            )
            ctr_time.ci_count_edges_term = config.internal_timebase
            logger_time.timing.cfg_samp_clk_timing(
                rate=config.estimated_fps,
                source=config.frame_clock_pin,
                active_edge=Edge.FALLING,
                sample_mode=AcquisitionType.CONTINUOUS,
            )
            logger_time.in_stream.input_buf_size = int(max(20000, config.estimated_fps * 3))

            encoder_channel = logger_enc.ci_channels.add_ci_ang_encoder_chan(
                counter=config.encoder_counter,
                decoding_type=EncoderType.X_4,
                units=AngleUnits.TICKS,
                pulses_per_rev=config.encoder_ppr,
                initial_angle=0.0,
            )
            encoder_channel.ci_encoder_a_input_term = config.encoder_a_pfi
            encoder_channel.ci_encoder_b_input_term = config.encoder_b_pfi
            logger_enc.timing.cfg_samp_clk_timing(
                rate=config.estimated_fps,
                source=config.frame_clock_pin,
                active_edge=Edge.FALLING,
                sample_mode=AcquisitionType.CONTINUOUS,
            )
            logger_enc.in_stream.input_buf_size = int(max(20000, config.estimated_fps * 3))

            for index, event in enumerate(config.output_events):
                if index >= len(config.output_counters):
                    self.status.emit(
                        f"Event {index + 1}: no free counter left, output marker will still be logged but no hardware pulse generated."
                    )
                    continue

                pulse_task = nidaqmx.Task()
                counter_name = config.output_counters[index]
                pulse_channel = pulse_task.co_channels.add_co_pulse_chan_time(
                    counter=counter_name,
                    units=TimeUnits.SECONDS,
                    idle_state=Level.LOW,
                    initial_delay=max(0.0, event.start_ms / 1000.0),
                    low_time=0.001,
                    high_time=max(0.0001, event.duration_ms / 1000.0),
                )
                pulse_channel.co_pulse_term = normalize_terminal(config.device_name, event.channel)
                pulse_task.timing.cfg_implicit_timing(sample_mode=AcquisitionType.FINITE, samps_per_chan=1)
                pulse_task.triggers.start_trigger.cfg_dig_edge_start_trig(config.frame_clock_pin, Edge.FALLING)
                pulse_tasks.append(pulse_task)

            save_folder = os.path.dirname(config.save_path)
            if save_folder:
                os.makedirs(save_folder, exist_ok=True)

            with open(config.save_path, "w", newline="") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(dynamic_header)

                logger_enc.start()
                logger_time.start()
                logger_data.start()
                for pulse_task in pulse_tasks:
                    pulse_task.start()
                relay.start()

                self.status.emit("Sending microscope start pulse (100 ms)...")
                with nidaqmx.Task() as start_task:
                    start_task.do_channels.add_do_chan(config.microscope_start_line)
                    start_task.write(False)
                    start_task.write(True)
                    time.sleep(0.1)
                    start_task.write(False)

                self.status.emit("Recording...")

                while not self._stop_event.is_set():
                    samples_available = logger_data.in_stream.avail_samp_per_chan

                    if samples_available == 0:
                        if config.stop_with_microscope and (time.time() - last_data_time) > config.silence_timeout_s and total_frames > 0:
                            stopped_by = "Microscope sync stopped"
                            break
                        time.sleep(0.002)
                        continue

                    chunk_data = logger_data.read(number_of_samples_per_channel=samples_available)
                    chunk_time = logger_time.read(number_of_samples_per_channel=samples_available)
                    chunk_enc = logger_enc.read(number_of_samples_per_channel=samples_available)
                    last_data_time = time.time()

                    if len(config.input_channels) == 1:
                        if not isinstance(chunk_data, list):
                            chunk_data = [chunk_data]
                        di_by_channel = [chunk_data]
                    else:
                        di_by_channel = chunk_data

                    if not isinstance(chunk_time, list):
                        chunk_time = [chunk_time]
                    if not isinstance(chunk_enc, list):
                        chunk_enc = [chunk_enc]

                    sample_count = min(
                        len(chunk_time),
                        len(chunk_enc),
                        *[len(channel_data) for channel_data in di_by_channel],
                    )

                    rows = []
                    reached_duration = False

                    for sample_index in range(sample_count):
                        total_frames += 1
                        curr_ticks_time = chunk_time[sample_index]
                        curr_ticks_enc_signed = parse_signed_32bit(chunk_enc[sample_index])

                        if start_ticks_time is None:
                            start_ticks_time = curr_ticks_time
                            start_ticks_enc = curr_ticks_enc_signed

                        exact_time_s = (curr_ticks_time - start_ticks_time) / config.timebase_freq

                        if (not config.stop_with_microscope) and (exact_time_s * 1000.0 >= config.expected_duration_ms):
                            reached_duration = True

                        current_dist_cm = (curr_ticks_enc_signed - start_ticks_enc) * cm_per_tick

                        history_buffer.append((exact_time_s, current_dist_cm))
                        while len(history_buffer) > 1 and (exact_time_s - history_buffer[0][0]) > config.smoothing_window_s:
                            history_buffer.popleft()

                        old_time, old_dist = history_buffer[0]
                        delta_t = exact_time_s - old_time
                        delta_d = current_dist_cm - old_dist
                        smoothed_velocity = (delta_d / delta_t) if delta_t > 0 else 0.0

                        input_values = [int(bool(di_by_channel[channel_index][sample_index])) for channel_index in range(len(di_by_channel))]
                        output_markers = []
                        time_ms = exact_time_s * 1000.0
                        for event in config.output_events:
                            marker = 1 if event.start_ms <= time_ms < (event.start_ms + event.duration_ms) else 0
                            output_markers.append(marker)

                        row = [
                            total_frames,
                            f"{exact_time_s:.6f}",
                            *input_values,
                            *output_markers,
                            curr_ticks_enc_signed,
                            f"{current_dist_cm:.4f}",
                            f"{smoothed_velocity:.4f}",
                        ]
                        rows.append(row)

                    if rows:
                        writer.writerows(rows)
                        self.velocity.emit(float(rows[-1][-1]))
                        self.frame_count.emit(total_frames)

                    now = time.time()
                    if now >= flush_deadline:
                        csv_file.flush()
                        flush_deadline = now + 1.0

                    if reached_duration:
                        stopped_by = "Expected duration reached"
                        break

                csv_file.flush()

            for pulse_task in pulse_tasks:
                try:
                    pulse_task.stop()
                except Exception:
                    pass
                pulse_task.close()

            if self._stop_event.is_set() and stopped_by == "Abort":
                stopped_by = "Aborted by user"

            self.finished.emit(f"Done. Frames: {total_frames}. Stop reason: {stopped_by}. File: {config.save_path}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NI Sync Recorder (USB-6421 / PCIe-6341)")
        self.resize(650, 532)

        self.base_config = RecorderConfig(save_path=self._default_csv_path())
        self.save_dir = os.getcwd()
        self.last_saved_path = None
        self.worker_thread = None
        self.worker = None

        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)

        top_box = QGroupBox("Control")
        top_layout = QHBoxLayout(top_box)

        control_col = QGroupBox("Run Controls")
        control_layout = QGridLayout(control_col)

        info_col = QGroupBox("Status / Info")
        info_layout = QVBoxLayout(info_col)

        self.mouse_id_edit = QLineEdit("A30")
        self.suffix_edit = QLineEdit("1")

        self.path_edit = QLineEdit(self.save_dir)
        self.path_edit.setReadOnly(True)
        browse_btn = QPushButton("Browse Dir")
        browse_btn.clicked.connect(self.on_browse)

        self.velocity_value = QLabel("0.0000 cm/s")
        self.velocity_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.velocity_value.setStyleSheet("font-size: 18px; font-weight: 600;")

        self.stop_with_sync_check = QCheckBox("Stop with microscope")
        self.stop_with_sync_check.setChecked(True)
        self.stop_with_sync_check.toggled.connect(self.on_stop_mode_changed)

        self.sync_frame_combo = QComboBox()
        self.sync_frame_combo.setEditable(True)
        self.sync_frame_combo.addItems(self._candidate_sync_channels())
        self.sync_frame_combo.setCurrentText(self._compact_pfi_label(self.base_config.frame_clock_pin))

        self.expected_ms_spin = QSpinBox()
        self.expected_ms_spin.setRange(1, 36000000)
        self.expected_ms_spin.setValue(5000)
        self.expected_ms_spin.setEnabled(False)

        self.start_btn = QPushButton("Start")
        self.abort_btn = QPushButton("Abort")
        self.view_btn = QPushButton("View")
        self.load_btn = QPushButton("Load")
        self.settings_btn = QPushButton("Settings")
        self.start_btn.clicked.connect(self.on_start)
        self.abort_btn.clicked.connect(self.on_abort)
        self.view_btn.clicked.connect(self.on_view)
        self.load_btn.clicked.connect(self.on_load)
        self.settings_btn.clicked.connect(self.on_settings)
        self.abort_btn.setEnabled(False)

        self.frame_state = QLabel("Frames: 0")
        self.status_panel = QTextEdit()
        self.status_panel.setReadOnly(True)
        self.channel_hint = QLabel(
            "Compact channel naming:\n"
            "- Sync/Output: PFI0 ... PFI15\n"
            "- Input: port0/line0 ... line15 (PFI aliases accepted)"
        )
        self.channel_hint.setWordWrap(True)

        control_layout.addWidget(QLabel("Mouse ID"), 0, 0)
        control_layout.addWidget(self.mouse_id_edit, 0, 1)
        control_layout.addWidget(QLabel("Suffix"), 0, 2)
        control_layout.addWidget(self.suffix_edit, 0, 3)

        control_layout.addWidget(QLabel("CSV Folder"), 1, 0)
        control_layout.addWidget(self.path_edit, 1, 1, 1, 3)
        control_layout.addWidget(browse_btn, 1, 4)

        control_layout.addWidget(QLabel("Current Velocity"), 2, 0)
        control_layout.addWidget(self.velocity_value, 2, 1, 1, 2)
        control_layout.addWidget(self.frame_state, 2, 3)

        control_layout.addWidget(self.stop_with_sync_check, 3, 0, 1, 2)
        control_layout.addWidget(QLabel("Sync frame"), 3, 2)
        control_layout.addWidget(self.sync_frame_combo, 3, 3)
        control_layout.addWidget(QLabel("Expected time (ms)"), 3, 4)
        control_layout.addWidget(self.expected_ms_spin, 3, 5)

        control_layout.addWidget(self.start_btn, 4, 0)
        control_layout.addWidget(self.abort_btn, 4, 1)
        control_layout.addWidget(self.view_btn, 4, 2)
        control_layout.addWidget(self.load_btn, 4, 3)
        control_layout.addWidget(self.settings_btn, 4, 4)

        info_layout.addWidget(self.status_panel)
        info_layout.addWidget(self.channel_hint)

        control_col.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        info_col.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        top_layout.addWidget(control_col, 3)
        top_layout.addWidget(info_col, 2)

        row2_layout = QHBoxLayout()

        out_group = QGroupBox("Output Events")
        out_layout = QVBoxLayout(out_group)
        self.output_table = QTableWidget(0, 4)
        self.output_table.setHorizontalHeaderLabels(["Channel", "Start (ms)", "Duration (ms)", "Delete"])
        self.output_table.horizontalHeader().setStretchLastSection(True)
        self.add_out_btn = QPushButton("Add event")
        self.add_out_btn.clicked.connect(self.add_output_row)
        out_layout.addWidget(self.output_table)
        out_layout.addWidget(self.add_out_btn)

        in_group = QGroupBox("Input Channels")
        in_layout = QVBoxLayout(in_group)
        self.input_table = QTableWidget(0, 5)
        self.input_table.setHorizontalHeaderLabels(["Channel", "Name", "Rate mode", "Rate (Hz)", "Delete"])
        self.input_table.horizontalHeader().setStretchLastSection(True)
        self.add_in_btn = QPushButton("Add input")
        self.add_in_btn.clicked.connect(self.add_input_row)
        self.safe_plan_label = QLabel(
            "Safe sampling plan: keep input channels on 'Follow Sync' so each row aligns to microscope frames."
        )
        self.safe_plan_label.setWordWrap(True)
        in_layout.addWidget(self.input_table)
        in_layout.addWidget(self.add_in_btn)
        in_layout.addWidget(self.safe_plan_label)

        row2_layout.addWidget(out_group)
        row2_layout.addWidget(in_group)

        root_layout.addWidget(top_box)
        root_layout.addLayout(row2_layout)

        self.add_output_row(default_channel="PFI1", default_start=2300, default_duration=10)
        self.add_output_row(default_channel="PFI1", default_start=2500, default_duration=10)
        self.add_input_row(default_channel=self.base_config.arduino_input_default, default_name="Arduino_State")
        self.update_save_folder_display()
        self.log_status("GUI", "Ready")
        self.check_hardware_state()

    def _default_csv_path(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.abspath(f"A30_1_{timestamp}.csv")

    def _build_csv_filename(self, timestamp=None):
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mouse_id = (self.mouse_id_edit.text().strip() or "Mouse").replace(" ", "_")
        suffix = (self.suffix_edit.text().strip() or "Session").replace(" ", "_")
        return f"{mouse_id}_{suffix}_{timestamp}.csv"

    def update_save_folder_display(self):
        self.path_edit.setText(os.path.abspath(self.save_dir))

    def log_status(self, category, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.status_panel.append(f"[{timestamp}] [{category}] {message}")

    def _candidate_sync_channels(self):
        channels = [f"PFI{i}" for i in range(16)]
        default_label = self._compact_pfi_label(self.base_config.frame_clock_pin)
        if default_label not in channels:
            channels.insert(0, default_label)
        return channels

    def _candidate_output_channels(self):
        return [f"PFI{i}" for i in range(16)]

    def _candidate_input_channels(self):
        return [f"port0/line{i}" for i in range(16)] + [f"PFI{i}" for i in range(16)]

    def _compact_pfi_label(self, terminal_text):
        terminal = terminal_text.strip().replace("\\", "/")
        if terminal.startswith("/"):
            terminal = terminal[1:]
        device_prefix = self.base_config.device_name + "/"
        if terminal.startswith(device_prefix):
            terminal = terminal[len(device_prefix):]
        return terminal

    def _resolve_sync_terminal(self, label_text):
        label = self._compact_pfi_label(label_text)
        if label.upper().startswith("PFI"):
            return f"/{self.base_config.device_name}/{label.upper()}"
        if label.startswith("/"):
            return label
        return label

    def _resolve_input_channel(self, label_text):
        text = label_text.strip().replace("\\", "/")
        if text.startswith("/"):
            text = text[1:]
        device_prefix = self.base_config.device_name + "/"
        if text.startswith(device_prefix):
            text = text[len(device_prefix):]

        upper = text.upper()
        if upper.startswith("PFI"):
            index_text = upper.replace("PFI", "")
            if index_text.isdigit():
                return f"{self.base_config.device_name}/port0/line{int(index_text)}"
        if text.startswith("port"):
            return f"{self.base_config.device_name}/{text}"
        if text.startswith(self.base_config.device_name + "/"):
            return text
        return text

    def on_stop_mode_changed(self, checked):
        self.expected_ms_spin.setEnabled(not checked)

    def on_browse(self):
        selected = QFileDialog.getExistingDirectory(self, "Select save folder", self.save_dir)
        if selected:
            self.save_dir = selected
            self.update_save_folder_display()
            self.log_status("GUI", f"Save folder set: {self.save_dir}")

    def on_view(self):
        selected = self.last_saved_path
        if not selected or (not os.path.exists(selected)):
            selected = self._find_most_recent_csv()
        if not selected:
            QMessageBox.information(self, "View", "No recent CSV found in the save folder.")
            self.log_status("GUI", "View requested but no recent CSV found")
            return
        viewer = LogViewerDialog(self)
        try:
            viewer.plot_csv(selected)
            self.log_status("GUI", f"Viewed most recent: {selected}")
        except Exception as exception:
            QMessageBox.critical(self, "View error", str(exception))
            self.log_status("GUI", f"View error: {exception}")
            viewer.close()
            return
        viewer.exec()

    def on_load(self):
        selected, _ = QFileDialog.getOpenFileName(self, "Load log CSV", self.save_dir, "CSV Files (*.csv)")
        if not selected:
            return
        viewer = LogViewerDialog(self)
        try:
            viewer.plot_csv(selected)
            self.log_status("GUI", f"Loaded file: {selected}")
        except Exception as exception:
            QMessageBox.critical(self, "Load error", str(exception))
            self.log_status("GUI", f"Load error: {exception}")
            viewer.close()
            return
        viewer.exec()

    def _find_most_recent_csv(self):
        if not os.path.isdir(self.save_dir):
            return None
        csv_paths = [
            os.path.join(self.save_dir, filename)
            for filename in os.listdir(self.save_dir)
            if filename.lower().endswith(".csv")
        ]
        if not csv_paths:
            return None
        return max(csv_paths, key=os.path.getmtime)

    def add_output_row(self, checked=False, default_channel="PFI1", default_start=2300, default_duration=50):
        row = self.output_table.rowCount()
        self.output_table.insertRow(row)

        channel_edit = QComboBox()
        channel_edit.setEditable(True)
        channel_edit.addItems(self._candidate_output_channels())
        channel_edit.setCurrentText(default_channel)
        start_spin = QSpinBox()
        start_spin.setRange(0, 36000000)
        start_spin.setValue(default_start)
        duration_spin = QSpinBox()
        duration_spin.setRange(1, 36000000)
        duration_spin.setValue(default_duration)

        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self.delete_output_row)

        self.output_table.setCellWidget(row, 0, channel_edit)
        self.output_table.setCellWidget(row, 1, start_spin)
        self.output_table.setCellWidget(row, 2, duration_spin)
        self.output_table.setCellWidget(row, 3, delete_btn)
        self.log_status("GUI", f"Output event row added ({default_channel}, {default_start} ms, {default_duration} ms)")

    def add_input_row(self, checked=False, default_channel="Dev1/port0/line3", default_name="Input"):
        row = self.input_table.rowCount()
        self.input_table.insertRow(row)

        channel_edit = QComboBox()
        channel_edit.setEditable(True)
        channel_edit.addItems(self._candidate_input_channels())
        channel_edit.setCurrentText(default_channel)
        name_edit = QLineEdit(default_name)
        mode_combo = QComboBox()
        mode_combo.addItems(["Follow Sync", "Fixed Hz"])
        rate_spin = QDoubleSpinBox()
        rate_spin.setRange(1.0, 100000.0)
        rate_spin.setDecimals(1)
        rate_spin.setValue(10000.0)
        rate_spin.setEnabled(False)
        mode_combo.currentTextChanged.connect(lambda text, spin=rate_spin: spin.setEnabled(text == "Fixed Hz"))

        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self.delete_input_row)

        self.input_table.setCellWidget(row, 0, channel_edit)
        self.input_table.setCellWidget(row, 1, name_edit)
        self.input_table.setCellWidget(row, 2, mode_combo)
        self.input_table.setCellWidget(row, 3, rate_spin)
        self.input_table.setCellWidget(row, 4, delete_btn)
        self.log_status("GUI", f"Input row added ({default_name}: {default_channel})")

    def delete_output_row(self):
        sender = self.sender()
        for row in range(self.output_table.rowCount()):
            if self.output_table.cellWidget(row, 3) is sender:
                self.output_table.removeRow(row)
                self.log_status("GUI", "Output event row deleted")
                return

    def delete_input_row(self):
        sender = self.sender()
        for row in range(self.input_table.rowCount()):
            if self.input_table.cellWidget(row, 4) is sender:
                self.input_table.removeRow(row)
                self.log_status("GUI", "Input row deleted")
                return

    def on_settings(self):
        dialog = SettingsDialog(self.base_config, self)
        if dialog.exec():
            dialog.apply_to_config()
            self.sync_frame_combo.setCurrentText(self._compact_pfi_label(self.base_config.frame_clock_pin))
            self.log_status("GUI", "Settings updated")
            self.check_hardware_state()

    def _collect_output_events(self):
        events = []
        for row in range(self.output_table.rowCount()):
            channel_widget = self.output_table.cellWidget(row, 0)
            start_widget = self.output_table.cellWidget(row, 1)
            duration_widget = self.output_table.cellWidget(row, 2)
            if not channel_widget:
                continue
            event = OutputEvent(
                channel=channel_widget.currentText().strip(),
                start_ms=int(start_widget.value()),
                duration_ms=int(duration_widget.value()),
            )
            events.append(event)
        return events

    def _collect_input_channels(self):
        channels = []
        for row in range(self.input_table.rowCount()):
            channel_widget = self.input_table.cellWidget(row, 0)
            name_widget = self.input_table.cellWidget(row, 1)
            mode_widget = self.input_table.cellWidget(row, 2)
            rate_widget = self.input_table.cellWidget(row, 3)
            if not channel_widget or not name_widget:
                continue
            rate_mode = "sync" if mode_widget.currentText() == "Follow Sync" else "fixed"
            channels.append(
                InputChannel(
                    channel=self._resolve_input_channel(channel_widget.currentText().strip()),
                    name=name_widget.text().strip() or f"Input_{row + 1}",
                    rate_mode=rate_mode,
                    fixed_rate_hz=float(rate_widget.value()),
                )
            )
        return channels

    def build_run_config(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_path = os.path.abspath(os.path.join(self.save_dir, self._build_csv_filename(timestamp=timestamp)))
        if not save_path:
            raise RuntimeError("Please select CSV save path.")

        config = RecorderConfig(
            save_path=save_path,
            device_name=self.base_config.device_name,
            frame_clock_pin=self._resolve_sync_terminal(self.sync_frame_combo.currentText().strip() or self.base_config.frame_clock_pin),
            microscope_start_line=self.base_config.microscope_start_line,
            arduino_trigger_pin=self.base_config.arduino_trigger_pin,
            arduino_input_default=self.base_config.arduino_input_default,
            encoder_counter=self.base_config.encoder_counter,
            time_counter=self.base_config.time_counter,
            encoder_a_pfi=self.base_config.encoder_a_pfi,
            encoder_b_pfi=self.base_config.encoder_b_pfi,
            internal_timebase=self.base_config.internal_timebase,
            timebase_freq=self.base_config.timebase_freq,
            estimated_fps=self.base_config.estimated_fps,
            wheel_diameter_cm=self.base_config.wheel_diameter_cm,
            encoder_ppr=self.base_config.encoder_ppr,
            smoothing_window_s=self.base_config.smoothing_window_s,
            silence_timeout_s=self.base_config.silence_timeout_s,
            stop_with_microscope=self.stop_with_sync_check.isChecked(),
            expected_duration_ms=self.expected_ms_spin.value(),
            output_events=self._collect_output_events(),
            input_channels=self._collect_input_channels(),
            output_counters=list(self.base_config.output_counters),
        )

        if not config.input_channels:
            raise RuntimeError("At least one input channel is required.")

        fixed = [channel for channel in config.input_channels if channel.rate_mode == "fixed"]
        if fixed:
            raise RuntimeError(
                "Safe sampling plan for this build: keep all input channels on Follow Sync. "
                "Fixed-rate channels need a second timing domain and frame alignment/resampling policy."
            )

        return config

    def check_hardware_state(self):
        try:
            devices = [device.name for device in System.local().devices]
            if not devices:
                self.log_status("HW", "No NI device found")
                return
            if self.base_config.device_name in devices:
                self.log_status("HW", f"{self.base_config.device_name} detected")
            else:
                self.log_status("HW", "Found devices: " + ", ".join(devices))
            self.log_status(
                "HW",
                "Input choices: " + ", ".join(self._candidate_input_channels()[:8]) + " ..."
            )
            self.log_status(
                "HW",
                "Output choices: " + ", ".join(self._candidate_output_channels()[:8]) + " ..."
            )
        except Exception as exception:
            self.log_status("HW", f"Check failed: {exception}")

    def on_start(self):
        try:
            config = self.build_run_config()
        except Exception as exception:
            QMessageBox.critical(self, "Config error", str(exception))
            return

        self.last_saved_path = config.save_path
        self.log_status("GUI", f"Output file name: {os.path.basename(config.save_path)}")

        self.start_btn.setEnabled(False)
        self.abort_btn.setEnabled(True)
        self.log_status("SW", f"Starting run -> {config.save_path}")
        self.velocity_value.setText("0.0000 cm/s")
        self.frame_state.setText("Frames: 0")

        self.worker_thread = QThread()
        self.worker = NIRecorderWorker(config)
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.velocity.connect(self.on_velocity)
        self.worker.frame_count.connect(self.on_frame_count)
        self.worker.status.connect(self.on_status)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.on_finished)

        self.worker.error.connect(self.cleanup_worker)
        self.worker.finished.connect(self.cleanup_worker)
        self.worker_thread.start()

    def on_abort(self):
        if self.worker is not None:
            self.worker.request_stop()
            self.log_status("SW", "Aborting...")

    def on_status(self, message):
        self.log_status("SW", message)

    def on_velocity(self, value):
        self.velocity_value.setText(f"{value:.4f} cm/s")

    def on_frame_count(self, count):
        self.frame_state.setText(f"Frames: {count}")

    def on_error(self, message):
        QMessageBox.critical(self, "Recorder error", message)
        self.log_status("SW", f"Error: {message}")

    def on_finished(self, message):
        QMessageBox.information(self, "Recorder", message)
        self.log_status("SW", message)

    def cleanup_worker(self):
        self.start_btn.setEnabled(True)
        self.abort_btn.setEnabled(False)
        if self.worker_thread is not None:
            self.worker_thread.quit()
            self.worker_thread.wait(2000)
            self.worker_thread = None
        self.worker = None


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
