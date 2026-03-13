import nidaqmx
import time
import csv
import sys
import os
import math
import shutil
from collections import deque
from datetime import datetime
from nidaqmx.constants import Edge, Signal, AcquisitionType, LineGrouping, CountDirection, EncoderType, AngleUnits

# --- CONFIGURATION ---
Mouse_ID             = "A35_puff_104"
SAVE_DIR             = os.path.expanduser("~/NI_logs")

# USB-6421 routing note (verify exact pinout in NI MAX for your hardware revision):
# - PFI and port0/line aliases are the same physical digital terminals.
# - Example aliases used in this file:
#   /Dev1/PFI0  <-> Dev1/port0/line0
#   /Dev1/PFI1  <-> Dev1/port0/line1
#   /Dev1/PFI14 <-> Dev1/port0/line14
#   /Dev1/PFI15 <-> Dev1/port0/line15
#
# Existing hardware usage (kept unchanged):
# - FRAME_CLOCK_PIN      = /Dev1/PFI0
# - ARDUINO_TRIG_PIN     = /Dev1/PFI1
# - MICROSCOPE_START_PIN = Dev1/port0/line2
# - ARDUINO_LINE         = Dev1/port0/line3
# - Encoder A/B          = /Dev1/PFI4, /Dev1/PFI5
#
# Camera lines below are assigned to free terminals to avoid conflicts.
ARDUINO_LINE         = "Dev1/port0/line3" 
CAMERA_SYNC_LINE     = "Dev1/port0/line14"  # /Dev1/PFI14 (camera strobe -> NI input)
CAMERA_SYNC_COUNTER_PIN = "/Dev1/PFI14"
FRAME_CLOCK_PIN      = "/Dev1/PFI0"
MICROSCOPE_START_PIN = "Dev1/port0/line2" 
CAMERA_TRIGGER_PIN   = "Dev1/port0/line15"  # /Dev1/PFI15 (NI trigger -> camera OPTO_IN)
ARDUINO_TRIG_PIN     = "/Dev1/PFI1"
MICROSCOPE_PULSE_S   = 0.100
CAMERA_SYNC_ACTIVE_EDGE = "falling"          # with pull-up, camera pulse is active-low
RECORD_DURATION_S    = 5.0                    # Set None to use silence-stop fallback only


def set_camera_trigger_level(task: nidaqmx.Task, high: bool):
    task.write(bool(high))


def _edge_from_config(edge_name: str):
    return Edge.FALLING if str(edge_name).strip().lower() == "falling" else Edge.RISING

# --- ENCODER CONFIGURATION ---
WHEEL_DIAMETER_CM    = 15.5
ENCODER_PPR          = 1024   
DECODING_TYPE        = EncoderType.X_4 

# --- VELOCITY SMOOTHING ---
SMOOTHING_WINDOW_S   = 0.05  # 50ms window

# Calculated Constants
TICKS_PER_REV        = ENCODER_PPR * 4
CM_PER_REV           = WHEEL_DIAMETER_CM * math.pi
CM_PER_TICK          = CM_PER_REV / TICKS_PER_REV

# --- INTERNAL CLOCK ---
INTERNAL_TIMEBASE    = "/Dev1/100MHzTimebase" 
TIMEBASE_FREQ        = 100000000.0 
SILENCE_TIMEOUT      = 0.3

# --- HELPER: FIX 32-BIT OVERFLOW ---
def parse_signed_32bit(n):
    """
    Converts unsigned 32-bit integers to signed python integers.
    4294967295 becomes -1
    """
    n = int(n) # Ensure it's a python int
    if n >= (1 << 31): # If bit 31 is set (looks like 2 billion+)
        n -= (1 << 32) # Subtract 2^32 to get negative value
    return n

def run_locomotion_logger():
    print("\n--- NI LOCOMOTION RECORDER (FIXED) ---")
    print("-> Features: Signed Integer Correction + Camera Trigger + Camera Frame Counting")
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{Mouse_ID}_{timestamp}.csv"
    os.makedirs(SAVE_DIR, exist_ok=True)
    full_path = os.path.abspath(os.path.join(SAVE_DIR, filename))
    ESTIMATED_FPS = 7000.0 

    with nidaqmx.Task() as relay, \
         nidaqmx.Task() as logger_data, \
         nidaqmx.Task() as logger_time, \
         nidaqmx.Task() as logger_enc, \
            nidaqmx.Task() as logger_camcnt, \
            nidaqmx.Task() as camera_trigger_task:
        
        # 1. SETUP RELAY
        relay.ai_channels.add_ai_voltage_chan("Dev1/ai0")
        relay.timing.cfg_samp_clk_timing(rate=1000, sample_mode=AcquisitionType.CONTINUOUS)
        relay.triggers.start_trigger.cfg_dig_edge_start_trig(FRAME_CLOCK_PIN, Edge.FALLING)
        relay.export_signals.export_signal(Signal.START_TRIGGER, ARDUINO_TRIG_PIN)
        
        # 2. SETUP DATA LOGGER
        logger_data.di_channels.add_di_chan(f"{ARDUINO_LINE},{CAMERA_SYNC_LINE}", line_grouping=LineGrouping.CHAN_PER_LINE)
        logger_data.timing.cfg_samp_clk_timing(rate=ESTIMATED_FPS, source=FRAME_CLOCK_PIN, active_edge=Edge.FALLING, sample_mode=AcquisitionType.CONTINUOUS)

        # 3. SETUP TIME LOGGER
        ctr_time = logger_time.ci_channels.add_ci_count_edges_chan(counter="Dev1/ctr0", edge=Edge.RISING, initial_count=0, count_direction=CountDirection.COUNT_UP)
        ctr_time.ci_count_edges_term = INTERNAL_TIMEBASE
        logger_time.timing.cfg_samp_clk_timing(rate=ESTIMATED_FPS, source=FRAME_CLOCK_PIN, active_edge=Edge.FALLING, sample_mode=AcquisitionType.CONTINUOUS)

        # 4. SETUP ENCODER LOGGER
        encoder_channel = logger_enc.ci_channels.add_ci_ang_encoder_chan(
            counter="Dev1/ctr2",
            decoding_type=DECODING_TYPE,
            units=AngleUnits.TICKS,
            pulses_per_rev=ENCODER_PPR,
            initial_angle=0.0
        )
        encoder_channel.ci_encoder_a_input_term = "/Dev1/PFI4"
        encoder_channel.ci_encoder_b_input_term = "/Dev1/PFI5"
        logger_enc.timing.cfg_samp_clk_timing(rate=ESTIMATED_FPS, source=FRAME_CLOCK_PIN, active_edge=Edge.FALLING, sample_mode=AcquisitionType.CONTINUOUS)

        # 5. SETUP CAMERA SYNC EDGE COUNTER (robust against frame-clock phase mismatch)
        use_counter_sync = True
        try:
            cam_counter = logger_camcnt.ci_channels.add_ci_count_edges_chan(
                counter="Dev1/ctr1",
                edge=_edge_from_config(CAMERA_SYNC_ACTIVE_EDGE),
                initial_count=0,
                count_direction=CountDirection.COUNT_UP,
            )
            cam_counter.ci_count_edges_term = CAMERA_SYNC_COUNTER_PIN
        except Exception as exc:
            use_counter_sync = False
            print(f"[WARN] Camera sync counter setup failed: {exc}")
            print("[WARN] Falling back to sampled digital-line edge detection.")

        # 6. SETUP CAMERA TRIGGER GATE OUTPUT (level mode on same pin)
        camera_trigger_task.do_channels.add_do_chan(CAMERA_TRIGGER_PIN)
        set_camera_trigger_level(camera_trigger_task, high=False)

        with open(full_path, "w", newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Frame_ID", "Time_s", "Arduino_State", "Camera_Frame", "Raw_Ticks_Signed", "Zeroed_Dist_cm", "Smoothed_Vel_cm_s"]) 
            
            print(f"STATUS: Saving to {full_path}...")
            print("STATUS: Arming System...")
            
            logger_enc.start()
            logger_time.start()
            logger_data.start()
            if use_counter_sync:
                logger_camcnt.start()
            relay.start()
            
            print("ACTION: Sending microscope start pulse and asserting camera trigger HIGH...")
            with nidaqmx.Task() as t_scope:
                t_scope.do_channels.add_do_chan(MICROSCOPE_START_PIN)
                t_scope.write(False)
                t_scope.write(True)
                # Level-trigger camera start on the same line; keep HIGH until acquisition ends.
                set_camera_trigger_level(camera_trigger_task, high=True)
                time.sleep(max(0.001, float(MICROSCOPE_PULSE_S)))
                t_scope.write(False)

            print("STATUS: Recording... (Waiting for frames)")
            
            total_frames = 0
            camera_frame_count = 0
            prev_camera_sync = None
            last_data_time = time.time()
            start_ticks_time = None 
            start_ticks_enc = None
            warned_no_camera_sync = False
            
            history_buffer = deque() 
            
            try:
                while True:
                    samples_available = logger_data.in_stream.avail_samp_per_chan
                    
                    if samples_available == 0:
                        if (time.time() - last_data_time) > SILENCE_TIMEOUT and total_frames > 0:
                            print(f"\n[STOP] Silence detected. Saving...")
                            break
                        time.sleep(0.005)
                        continue
                    
                    chunk_data = logger_data.read(number_of_samples_per_channel=samples_available)
                    chunk_time = logger_time.read(number_of_samples_per_channel=samples_available)
                    chunk_enc  = logger_enc.read(number_of_samples_per_channel=samples_available)
                    
                    last_data_time = time.time()
                    
                    if not isinstance(chunk_data, list): chunk_data = [chunk_data]
                    if not isinstance(chunk_time, list): chunk_time = [chunk_time]
                    if not isinstance(chunk_enc, list):  chunk_enc  = [chunk_enc]

                    if len(chunk_data) == 2 and isinstance(chunk_data[0], list):
                        chunk_arduino = chunk_data[0]
                        chunk_camera_sync = chunk_data[1]
                    elif len(chunk_data) == 1 and isinstance(chunk_data[0], list):
                        chunk_arduino = chunk_data[0]
                        chunk_camera_sync = [0] * len(chunk_arduino)
                    else:
                        chunk_arduino = chunk_data
                        chunk_camera_sync = [0] * len(chunk_arduino)

                    sample_count = min(len(chunk_arduino), len(chunk_camera_sync), len(chunk_time), len(chunk_enc))
                    if use_counter_sync:
                        try:
                            camera_frame_count = int(logger_camcnt.read())
                        except Exception:
                            pass
                    
                    rows = []
                    stop_due_to_duration = False
                    for i in range(sample_count):
                        curr_ticks_time = chunk_time[i]

                        # Zeroing
                        if start_ticks_time is None:
                            start_ticks_time = curr_ticks_time
                            start_ticks_enc = parse_signed_32bit(chunk_enc[i])
                        
                        exact_time_s = (curr_ticks_time - start_ticks_time) / TIMEBASE_FREQ
                        if RECORD_DURATION_S is not None and exact_time_s > float(RECORD_DURATION_S):
                            stop_due_to_duration = True
                            break

                        total_frames += 1

                        state = int(chunk_arduino[i])
                        camera_sync_state = int(chunk_camera_sync[i])

                        if not use_counter_sync:
                            if prev_camera_sync is not None:
                                if CAMERA_SYNC_ACTIVE_EDGE == "falling":
                                    if prev_camera_sync == 1 and camera_sync_state == 0:
                                        camera_frame_count += 1
                                else:
                                    if prev_camera_sync == 0 and camera_sync_state == 1:
                                        camera_frame_count += 1
                            prev_camera_sync = camera_sync_state
                        
                        # --- FIX 1: Convert to Signed Integer ---
                        curr_ticks_enc_signed = parse_signed_32bit(chunk_enc[i])
                        
                        # Correct distance calculation using signed math
                        current_dist_cm = (curr_ticks_enc_signed - start_ticks_enc) * CM_PER_TICK
                        
                        # Sliding Window Velocity
                        history_buffer.append((exact_time_s, current_dist_cm))
                        while len(history_buffer) > 1 and (exact_time_s - history_buffer[0][0]) > SMOOTHING_WINDOW_S:
                            history_buffer.popleft()
                        
                        old_time, old_dist = history_buffer[0]
                        delta_t = exact_time_s - old_time
                        delta_d = current_dist_cm - old_dist
                        
                        if delta_t > 0:
                            smoothed_velocity = delta_d / delta_t
                        else:
                            smoothed_velocity = 0.0
                            
                        rows.append([
                            total_frames, 
                            f"{exact_time_s:.6f}", 
                            state, 
                            camera_frame_count,
                            curr_ticks_enc_signed,   # Now saves -1 instead of 4294967295       
                            f"{current_dist_cm:.4f}", 
                            f"{smoothed_velocity:.4f}"        
                        ])
                    
                    writer.writerows(rows)

                    if not rows:
                        if stop_due_to_duration:
                            print(f"\n[STOP] Duration reached ({RECORD_DURATION_S:.3f}s). Saving...")
                            break
                        continue
                    
                    term_cols = shutil.get_terminal_size(fallback=(120, 20)).columns
                    status_str = (
                        f"\rScopeFr: {total_frames} | CamFr: {rows[-1][3]} | "
                        f"T: {rows[-1][1]}s | Pos: {rows[-1][5]}cm | Vel: {rows[-1][6]}cm/s"
                    )
                    # Clip and pad to terminal width to avoid wrapping artifacts and stale chars.
                    max_line = max(20, term_cols - 1)
                    sys.stdout.write(status_str[:max_line].ljust(max_line))
                    sys.stdout.flush()

                    if (not warned_no_camera_sync) and total_frames >= 500 and camera_frame_count == 0:
                        warned_no_camera_sync = True
                        print("\n[WARN] No camera sync edges detected on CAMERA_SYNC_LINE.")
                        print("[WARN] Check camera sync output line mapping (camera LineX -> NI PFI14), edge polarity, and pulse width.")

                    if stop_due_to_duration:
                        print(f"\n[STOP] Duration reached ({RECORD_DURATION_S:.3f}s). Saving...")
                        break
                    
                    time.sleep(0.01)
                    
            except KeyboardInterrupt:
                print("\nStopped.")

            print("\nACTION: Deasserting camera trigger (LOW) on same line...")
            set_camera_trigger_level(camera_trigger_task, high=False)

    print(f"\n\n--- DONE ---")
    print(f"Microscope Frames:      {total_frames}")
    print(f"Camera Frames Detected: {camera_frame_count}")
    print(f"File Saved:   {full_path}")

if __name__ == "__main__":
    run_locomotion_logger()