from direct.showbase.ShowBase import ShowBase
from direct.gui.OnscreenText import OnscreenText
from panda3d.core import loadPrcFileData, AmbientLight, DirectionalLight, Vec4
from direct.task import Task
import serial
import time
from threading import Thread
import sys
import os 
import datetime

# --- 1. AUTOMATIC DIRECTORY FIX ---
script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)
print(f"Working Directory set to: {script_dir}")

# --- CONFIGURATION ---
COM_PORT = 'COM3'      
BAUD_RATE = 115200     

# --- VR WORLD SETTINGS (Units = cm) ---
TRACK_START_Y = 0      
TRACK_END_Y   = 200    
REWARD_Y      = 180    # Trigger Valve Here
GAIN_VAL      = 0.122    

# --- DATA LOGGING SETTINGS ---
LOG_INTERVAL = 0.1     # Save CSV every 0.1s (100ms)
MOUSE_ID = "TestMouse" 
SESSION_ID = "S1"

# Setup Window
loadPrcFileData('', 'win-size 1920 1080') 
loadPrcFileData('', 'window-title 200cm VR Track')

# --- SHARED MEMORY ---
# [Encoder_Val, Lick_State(0/1)]
arduino_vals = [0, 0] 
program_running = True
ser = None # Global Serial Object

# --- SERIAL LISTENER THREAD ---
def serial_listener():
    global ser
    try:
        print(f"Connecting to {COM_PORT}...")
        ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
        time.sleep(2)
        print("Arduino Connected!")
        
        while program_running:
            if ser.in_waiting:
                try:
                    line = ser.readline().decode('utf-8').rstrip()
                    
                    # CASE A: Position Update
                    if line.startswith("P:"):
                        raw_number = line.split(":")[1] 
                        arduino_vals[0] = float(raw_number)
                    
                    # CASE B: Lick Detected
                    elif line == "LICK":
                        print(">> LICK! <<")
                        arduino_vals[1] = 1 # Set Lick Flag High
                        
                except ValueError:
                    pass 
    except Exception as e:
        print(f"SERIAL ERROR: {e}")

# --- MAIN VR CLASS ---
class VR_Environment(ShowBase):
    def __init__(self):
        super().__init__()

        # --- INIT DATA VARIABLES ---
        self.date_str = datetime.datetime.now().strftime("%Y%m%d")
        self.start_time = time.time()
        self.csv_filename = f"Log_{self.date_str}_{MOUSE_ID}_{SESSION_ID}.csv"
        
        # Initialize CSV with Header
        with open(self.csv_filename, "w") as f:
            f.write("Time,PosY,PosX,RewardTimer,Lick\n")

        # Trial Data Storage (For Friend's Format)
        self.this_trial_data = [] 
        self.lap_count = 1
        
        # Flags
        self.game_started = False
        self.reward_given_this_lap = False
        self.reward_timer = 0 # 0 = No Reward, >0 = Reward Active

        # --- SCENE SETUP ---
        self.setBackgroundColor(0, 0, 0)
        self.render.hide()
        
        try:
            self.scene = self.loader.loadModel("models/track_200cm.bam")
            self.scene.reparentTo(self.render)
            self.scene.setPos(0, 0, 0) 
        except Exception:
            print("Model not found, using empty space.")
        
        # Camera
        self.disableMouse() 
        self.camera.setPos(0, TRACK_START_Y, 2)
        self.camLens.setFov(120)

        # Lighting
        alight = AmbientLight('alight')
        alight.setColor(Vec4(0.5, 0.5, 0.5, 1))
        self.render.setLight(self.render.attachNewNode(alight))

        # UI Text
        self.start_text = OnscreenText(text="READY\nCtrl+P to Start", pos=(-0.0, 0.0), scale=0.07, fg=(1,1,1,1))
        self.lap_text = OnscreenText(text=f"Laps: {self.lap_count}", pos=(-1.3, 0.9), scale=0.07, fg=(1,1,1,1))
        self.lap_text.hide()

        # Keys
        self.accept('escape', sys.exit)
        self.accept('control-p', self.start_the_game)

        # Init Logic
        self.last_encoder_val = arduino_vals[0]
        self.taskMgr.add(self.update_movement, "MovementTask")
        
        # Add Data Logging Task (Every 100ms)
        self.taskMgr.doMethodLater(LOG_INTERVAL, self.log_data_task, "DataLogTask")

    def start_the_game(self):
        if not self.game_started:
            self.game_started = True
            self.render.show()
            self.start_text.destroy()
            self.lap_text.show()
            self.last_encoder_val = arduino_vals[0]
            print("GAME STARTED")

    def log_data_task(self, task):
        if self.game_started:
            # 1. Gather Data
            t = round(time.time() - self.start_time, 3)
            pos_y = round(self.camera.getY(), 2)
            pos_x = round(self.camera.getX(), 2)
            lick = arduino_vals[1]
            
            # 2. Append to CSV (Live Log)
            with open(self.csv_filename, "a") as f:
                f.write(f"{t},{pos_y},{pos_x},{self.reward_timer},{lick}\n")
            
            # 3. Append to Trial List (Friend's Format)
            # Format: [pos.y, pos.x, reward_timer, lick]
            # Note: We reset lick flag after reading
            self.this_trial_data.append([pos_y, pos_x, self.reward_timer, lick])
            
            # Reset Lick Flag (so we don't count the same lick twice)
            if lick == 1:
                arduino_vals[1] = 0
                
            # Manage Reward Timer (Just for logging visualization)
            if self.reward_timer > 0:
                self.reward_timer -= 1

        return Task.again # Schedule to run again in 100ms

    def save_trial_txt(self):
        # Format filename like friend's: Date_MouseID_Session_trialX.txt
        filename = f"{self.date_str}_{MOUSE_ID}_{SESSION_ID}_trial{self.lap_count}.txt"
        path = os.path.join(script_dir, filename)
        
        with open(path, "w") as f:
            f.write(str(self.this_trial_data))
        
        print(f"Saved Trial Data: {filename}")
        self.this_trial_data = [] # Clear for next lap

    def update_movement(self, task):
        if not self.game_started: return Task.cont

        # Movement Logic
        current_encoder_val = arduino_vals[0]
        delta = current_encoder_val - self.last_encoder_val
        self.last_encoder_val = current_encoder_val

        if abs(delta) < 500: 
            move_amount = -(delta / 10.0) * GAIN_VAL
            current_y = self.camera.getY()
            new_y = current_y + move_amount
            
            # --- REWARD LOGIC ---
            if new_y >= REWARD_Y and not self.reward_given_this_lap:
                print(">> REWARD TRIGGERED <<")
                if ser: ser.write(b'R') # Send 'R' to Arduino
                self.reward_given_this_lap = True
                self.reward_timer = 5 # Mark roughly 500ms in logs (5 * 100ms)

            # --- LAP RESET LOGIC ---
            if new_y >= TRACK_END_Y:
                # 1. Save Previous Lap Data
                self.save_trial_txt()
                
                # 2. Reset VR
                self.lap_count += 1
                self.lap_text.setText(f"Laps: {self.lap_count}")
                new_y = TRACK_START_Y 
                self.reward_given_this_lap = False
                
            elif new_y < TRACK_START_Y:
                new_y = TRACK_START_Y
            
            self.camera.setY(new_y)

        return Task.cont

if __name__ == "__main__":
    msg_thread = Thread(target=serial_listener)
    msg_thread.daemon = True
    msg_thread.start()

    game = VR_Environment()
    game.run()