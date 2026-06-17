#define TRIGGER_PIN 2   // Input from NI (PFI 1)
#define FEEDBACK_PIN 1  // Output TO NI (PFI 3 / Pin 29)
#define PUFF_PIN    10   // Air Puff

// --- TIMING CONFIG (ms) ---
const unsigned long FIRST_PUFF_DELAY_MS = 10000;
const unsigned long PUFF_INTERVAL_MS = 5000;
const unsigned long STIM_DURATION_MS = 31000;
const unsigned long AIR_PUFF_WIDTH_MS = 10;  // Default air-puff width
const unsigned long TRIGGER_REARM_DELAY_MS = 40000; // Block new trigger for full recording window

// --- STATE ---
volatile bool triggerReceived = false;
volatile unsigned long triggerTime = 0;
volatile bool triggerArmed = true;

unsigned long nextPuffElapsedMs = FIRST_PUFF_DELAY_MS;
unsigned long pulseStartTime = 0;
bool pulseActive = false;

void setup() {
  pinMode(TRIGGER_PIN, INPUT);
  pinMode(FEEDBACK_PIN, OUTPUT); // New Feedback Pin
  pinMode(PUFF_PIN, OUTPUT);
  
  // Ensure feedback starts Low
  digitalWrite(FEEDBACK_PIN, LOW);
  digitalWrite(PUFF_PIN, LOW);

  // Interrupt on Rising Edge (Standard for NI triggers)
  attachInterrupt(digitalPinToInterrupt(TRIGGER_PIN), onTrigger, RISING);
}

void loop() {
  // Re-arm only after trigger is LOW and lockout window has passed.
  unsigned long now = millis();
  unsigned long t0ForRearm;
  noInterrupts();
  t0ForRearm = triggerTime;
  interrupts();

  if (!triggerReceived && !triggerArmed &&
      digitalRead(TRIGGER_PIN) == LOW &&
      (now - t0ForRearm) >= TRIGGER_REARM_DELAY_MS) {
    triggerArmed = true;
  }

  if (triggerReceived) {
    unsigned long t0;
    noInterrupts();
    t0 = triggerTime;
    interrupts();
    unsigned long elapsed = millis() - t0;

    // End stimulation train at configured duration.
    if (elapsed >= STIM_DURATION_MS) {
      triggerReceived = false;
      pulseActive = false;
      nextPuffElapsedMs = FIRST_PUFF_DELAY_MS;
      digitalWrite(FEEDBACK_PIN, LOW);
      digitalWrite(PUFF_PIN, LOW);
      return;
    }

    // Start a new synchronized feedback + puff pulse every 5 s.
    if (!pulseActive && elapsed >= nextPuffElapsedMs) {
      digitalWrite(FEEDBACK_PIN, HIGH);
      digitalWrite(PUFF_PIN, HIGH);
      pulseStartTime = millis();
      pulseActive = true;
      nextPuffElapsedMs += PUFF_INTERVAL_MS;
    }

    // End active pulse after configured width.
    if (pulseActive && (millis() - pulseStartTime) >= AIR_PUFF_WIDTH_MS) {
      digitalWrite(FEEDBACK_PIN, LOW);
      digitalWrite(PUFF_PIN, LOW);
      pulseActive = false;
    }
  }
}

void onTrigger() {
  if (triggerArmed && !triggerReceived) {
    triggerReceived = true;
    triggerArmed = false;
    triggerTime = millis();

    // Reset pulse train state on each start trigger.
    nextPuffElapsedMs = FIRST_PUFF_DELAY_MS;
    pulseActive = false;
    digitalWrite(FEEDBACK_PIN, LOW);
    digitalWrite(PUFF_PIN, LOW);
  }
}