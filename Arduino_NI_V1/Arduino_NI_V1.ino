// Arduino_NI_MEGA_V3 — Hardware Interrupt Relay Model
// Designed for Arduino Mega 2560 R3

// --- PIN MAPPING ---
#define BUZZ_CMD_PIN 2   // NI PFI6 (Hardware Interrupt 0)
#define PUFF_CMD_PIN 3   // NI PFI7 (Hardware Interrupt 1)
#define FEEDBACK_PIN 13   // Feedback TO NI PFI3 (Safe from Serial)
#define BUZZER_PIN   12   // 4kHz Tone
#define PUFF_PIN     10  // Air Puff Solenoid

// --- DURATIONS ---
#define BUZZ_FIXED_MS 150UL
#define PUFF_FIXED_MS 20UL

// --- VOLATILE FLAGS (Interrupt-Safe) ---
volatile bool buzzTriggered = false;
volatile bool puffTriggered = false;

unsigned long buzzEndMs = 0;
unsigned long puffEndMs = 0;

void setup() {
  // Serial for debugging - ensure Monitor is set to 115200
  Serial.begin(115200);
  Serial.println("--- MEGA SYSTEM READY ---");
  Serial.println("Listening on Pin 2 (Buzz) and Pin 3 (Puff)...");

  pinMode(BUZZ_CMD_PIN, INPUT);
  pinMode(PUFF_CMD_PIN, INPUT);
  pinMode(FEEDBACK_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(PUFF_PIN, OUTPUT);

  digitalWrite(FEEDBACK_PIN, LOW);
  digitalWrite(PUFF_PIN, LOW);
  noTone(BUZZER_PIN);

  // Attach Hardware Interrupts (Specific to Mega Pins 2 and 3)
  attachInterrupt(digitalPinToInterrupt(BUZZ_CMD_PIN), onBuzzInterrupt, RISING);
  attachInterrupt(digitalPinToInterrupt(PUFF_CMD_PIN), onPuffInterrupt, RISING);
}

void loop() {
  unsigned long nowMs = millis();

  // 1. Process Buzz Trigger
  if (buzzTriggered) {
    noInterrupts();
    buzzTriggered = false;
    interrupts();
    buzzEndMs = nowMs + BUZZ_FIXED_MS;
    Serial.println("NI Trigger: Buzzing (150ms)");
  }

  // 2. Process Puff Trigger
  if (puffTriggered) {
    noInterrupts();
    puffTriggered = false;
    interrupts();
    puffEndMs = nowMs + PUFF_FIXED_MS;
    Serial.println("NI Trigger: Puffing (20ms)");
  }

  // 3. Timing Logic
  bool buzzActive = (nowMs < buzzEndMs);
  bool puffActive = (nowMs < puffEndMs);

  // 4. Actuator Control
  if (buzzActive) {
    tone(BUZZER_PIN, 4000); // 4kHz
  } else {
    noTone(BUZZER_PIN);
    buzzEndMs = 0; // Prevent overflow issues
  }

  if (puffActive) {
    digitalWrite(PUFF_PIN, HIGH);
  } else {
    digitalWrite(PUFF_PIN, LOW);
    puffEndMs = 0;
  }

  // 5. Feedback to NI (HIGH if either is active)
  digitalWrite(FEEDBACK_PIN, (buzzActive || puffActive) ? HIGH : LOW);
}

// --- INTERRUPT SERVICE ROUTINES ---
void onBuzzInterrupt() {
  buzzTriggered = true;
}

void onPuffInterrupt() {
  puffTriggered = true;
}