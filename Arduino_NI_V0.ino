#define TRIGGER_PIN 2   // Input from NI (PFI 1)
#define FEEDBACK_PIN 1  // Output TO NI (PFI 3 / Pin 29)
#define BUZZER_PIN  0   // 10kHz Beep
#define PUFF_PIN    10   // Air Puff

// --- VARIABLES ---
volatile bool triggerReceived = false;
volatile unsigned long triggerTime = 0;

bool beepFired = false;
bool puffStarted = false;
bool puffEnded = false;

void setup() {
  pinMode(TRIGGER_PIN, INPUT);
  pinMode(FEEDBACK_PIN, OUTPUT); // New Feedback Pin
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(PUFF_PIN, OUTPUT);
  
  // Ensure feedback starts Low
  digitalWrite(FEEDBACK_PIN, LOW);

  // Interrupt on Rising Edge (Standard for NI triggers)
  attachInterrupt(digitalPinToInterrupt(TRIGGER_PIN), onTrigger, RISING);
}

void loop() {
  if (triggerReceived) {
    unsigned long elapsed = millis() - triggerTime;

    // -------------------------------------------------
    // 1. BEEP LOGIC (Start at 2300ms)
    // -------------------------------------------------
    if (elapsed >= 2300 && !beepFired) {
      // A. Send Feedback Pulse to NI Card first!
      digitalWrite(FEEDBACK_PIN, HIGH);
      
      // B. Start Sound
      tone(BUZZER_PIN, 4000, 250); 
      
      // C. Hold Feedback High for 2ms to ensure NI catches it
      delay(2); 
      digitalWrite(FEEDBACK_PIN, LOW);
      
      beepFired = true; 
    }

    // -------------------------------------------------
    // 2. PUFF LOGIC (Start at 2500ms)
    // -------------------------------------------------
    if (elapsed >= 2500 && !puffStarted) {
      // A. Send Feedback Pulse
      digitalWrite(FEEDBACK_PIN, HIGH);
      
      // B. Open Valve
      digitalWrite(PUFF_PIN, HIGH);
      
      // C. Hold Feedback High for 2ms
      delay(2);
      digitalWrite(FEEDBACK_PIN, LOW);
      
      puffStarted = true;
    }
    
    // Stop Puff at 2550ms
    if (elapsed >= 2510 && !puffEnded) {
      digitalWrite(PUFF_PIN, LOW);
      puffEnded = true;
    }

    // -------------------------------------------------
    // 3. RESET LOGIC (>6000ms)
    // -------------------------------------------------
    if (elapsed >= 6000) {
      triggerReceived = false; 
      beepFired = false;
      puffStarted = false;
      puffEnded = false;
      noTone(BUZZER_PIN);
      digitalWrite(PUFF_PIN, LOW);
    }
  }
}

void onTrigger() {
  if (!triggerReceived) {
    triggerReceived = true;
    triggerTime = millis();
    // Safety Reset
    beepFired = false;
    puffStarted = false;
    puffEnded = false;
  }
}