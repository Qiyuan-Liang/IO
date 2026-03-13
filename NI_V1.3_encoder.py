import nidaqmx
import time
import csv
import sys
import os
import math
from collections import deque
from datetime import datetime
from nidaqmx.constants import Edge, Signal, AcquisitionType, LineGrouping, CountDirection, EncoderType, AngleUnits

# --- CONFIGURATION ---
Mouse_ID             = "A35_PC_GrC_puff_13"
ARDUINO_LINE         = "Dev1/port0/line3" 
FRAME_CLOCK_PIN      = "/Dev1/PFI0"
MICROSCOPE_START_PIN = "Dev1/port0/line2" 
ARDUINO_TRIG_PIN     = "/Dev1/PFI1"

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
    print("-> Features: Signed Integer Correction + Screen Clearing")
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{Mouse_ID}_{timestamp}.csv"
    ESTIMATED_FPS = 7000.0 

    with nidaqmx.Task() as relay, \
         nidaqmx.Task() as logger_data, \
         nidaqmx.Task() as logger_time, \
         nidaqmx.Task() as logger_enc:
        
        # 1. SETUP RELAY
        relay.ai_channels.add_ai_voltage_chan("Dev1/ai0")
        relay.timing.cfg_samp_clk_timing(rate=1000, sample_mode=AcquisitionType.CONTINUOUS)
        relay.triggers.start_trigger.cfg_dig_edge_start_trig(FRAME_CLOCK_PIN, Edge.FALLING)
        relay.export_signals.export_signal(Signal.START_TRIGGER, ARDUINO_TRIG_PIN)
        
        # 2. SETUP DATA LOGGER
        logger_data.di_channels.add_di_chan(ARDUINO_LINE, line_grouping=LineGrouping.CHAN_PER_LINE)
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

        with open(filename, "w", newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Frame_ID", "Time_s", "Arduino_State", "Raw_Ticks_Signed", "Zeroed_Dist_cm", "Smoothed_Vel_cm_s"]) 
            
            print(f"STATUS: Saving to {filename}...")
            print("STATUS: Arming System...")
            
            logger_enc.start()
            logger_time.start()
            logger_data.start()
            relay.start()
            
            print("ACTION: Sending Start Pulse (100ms)...")
            with nidaqmx.Task() as t:
                t.do_channels.add_do_chan(MICROSCOPE_START_PIN)
                t.write(False); t.write(True); time.sleep(0.1); t.write(False)

            print("STATUS: Recording... (Waiting for frames)")
            
            total_frames = 0
            last_data_time = time.time()
            start_ticks_time = None 
            start_ticks_enc = None
            
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
                    
                    rows = []
                    for i in range(len(chunk_data)):
                        total_frames += 1
                        
                        curr_ticks_time = chunk_time[i]
                        state = int(chunk_data[i])
                        
                        # --- FIX 1: Convert to Signed Integer ---
                        curr_ticks_enc_signed = parse_signed_32bit(chunk_enc[i])
                        
                        # Zeroing
                        if start_ticks_time is None:
                            start_ticks_time = curr_ticks_time
                            start_ticks_enc = curr_ticks_enc_signed
                        
                        exact_time_s = (curr_ticks_time - start_ticks_time) / TIMEBASE_FREQ
                        
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
                            curr_ticks_enc_signed,   # Now saves -1 instead of 4294967295       
                            f"{current_dist_cm:.4f}", 
                            f"{smoothed_velocity:.4f}"        
                        ])
                    
                    writer.writerows(rows)
                    
                    # --- FIX 2: Pad Output string to clear artifacts ---
                    status_str = f"\rFr: {total_frames} | T: {rows[-1][1]}s | Pos: {rows[-1][4]}cm | Vel: {rows[-1][5]}cm/s"
                    # Add 20 spaces of padding to wipe out old text, then flush
                    sys.stdout.write(status_str.ljust(80)) 
                    sys.stdout.flush()
                    
                    time.sleep(0.01)
                    
            except KeyboardInterrupt:
                print("\nStopped.")

    full_path = os.path.abspath(filename)
    print(f"\n\n--- DONE ---")
    print(f"Total Frames: {total_frames}")
    print(f"File Saved:   {full_path}")

if __name__ == "__main__":
    run_locomotion_logger()