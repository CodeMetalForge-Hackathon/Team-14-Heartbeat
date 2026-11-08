// Heartbeat rover bridge: Me Auriga / ATmega2560, Land Raider assembly.
// Encoder interrupt setup and timer configuration follow the official
// Me_Auriga_encoder_direct example. No blocking motor/PID move is used.
#include <MeAuriga.h>
#include <EEPROM.h>
#include <util/atomic.h>
#include <ctype.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

// Board/motor facts from Makeblock's Auriga examples, not route tuning.
const uint8_t RING_PIN = 44;
const uint8_t RING_LEDS = 12;
const uint8_t BATTERY_PIN = A4;
const float BATTERY_FULL_SCALE_V = 15.0f;  // Nominal board divider, not a calibrated meter.
const float ENCODER_PULSES_PER_REV = 9.0f;
const float MOTOR_GEAR_RATIO = 39.267f;
const float PULSES_PER_WHEEL_DEG = ENCODER_PULSES_PER_REV * MOTOR_GEAR_RATIO / 360.0f;

// Fixed firmware safety ceilings. Host profile limits may be more restrictive.
const float MAX_DRIVE_CM = 100.0f;
const float MAX_TURN_DEG = 180.0f;
const float MAX_SPEED_PCT = 60.0f;
const float MIN_CALIBRATION = 0.001f;
const float MAX_CALIBRATION = 100000.0f;
const unsigned long WATCHDOG_MS = 1500;
const unsigned long STALL_MS = 1500;
const unsigned long MAX_MOVE_MS = 30000;
const unsigned long CONTROL_MS = 20;
const float PWM_SLEW_PER_S = 240.0f;  // Fixed maximum rise/fall rate for ordinary moves.
// Fixed completion-error ceilings: two encoder pulses AND at most 1% of a move.
// Short moves below 100 pulses still require the exact encoder target.
const long MAX_TARGET_ERROR_PULSES = 2;
const int FRICTION_PWM = 25;  // Makeblock's PWM_MIN_OFFSET; capped by requested speed.
const uint8_t MAX_LINE = 95;
const uint8_t RX_BUDGET = 64;  // Bound work per loop so a serial flood cannot starve safety.

// Startup fallback only. profiles/rubble.yaml mirrors these values; CAL persists
// measured values in EEPROM. The MCU cannot read a host YAML file.
const float DEFAULT_DEG_PER_CM = 18.0f;
const float DEFAULT_DEG_PER_TURN_DEG = 2.5f;
const uint32_t CAL_MAGIC = 0x48425231UL;  // HBR1; changing record shape requires a new magic.

// Mechanical orientation of this assembled Ranger: sensor-facing forward is
// SLOT1 negative / SLOT2 positive. Encoder progress uses the same signed mapping.
const int DRIVE_FORWARD_POLARITY = -1;

MeEncoderOnBoard leftMotor(SLOT1);
MeEncoderOnBoard rightMotor(SLOT2);
MeRGBLed ring(0, RING_LEDS);

struct __attribute__((packed)) CalibrationRecord {
  uint32_t magic;
  float degPerCm;
  float degPerTurnDeg;
  uint32_t checksum;
};

float degPerCm = DEFAULT_DEG_PER_CM;
float degPerTurnDeg = DEFAULT_DEG_PER_TURN_DEG;
char lineBuffer[MAX_LINE + 1];
uint8_t lineLength = 0;
bool discardLine = false;
bool moving = false;
bool turning = false;
char moveValue[MAX_LINE + 1];
long startLeft = 0, startRight = 0, targetPulses = 0;
long previousLeft = 0, previousRight = 0;
int leftDirection = 1, rightDirection = -1;
float maxPwm = 0.0f, leftPwm = 0.0f, rightPwm = 0.0f;
unsigned long lastCommandMs = 0, moveStartMs = 0, lastControlMs = 0;
unsigned long leftProgressMs = 0, rightProgressMs = 0;

// EXPLORE is opt-in and configured in RAM by NAVCFG; no motor action on boot.
const unsigned long RANGE_TIMEOUT_US = 24000;  // Bounded echo wait, about 4 m.
bool navigationConfigured = false, exploring = false;
enum NavigationPhase { NAV_WAIT, NAV_FORWARD, NAV_TURN, NAV_RETRY };
NavigationPhase navigationPhase = NAV_WAIT;
uint8_t rangePort = 0, navigationMaxTurns = 0, navigationTurns = 0;
float navigationStopCm = 0, navigationClearCm = 0, navigationStepCm = 0;
float navigationTurnDeg = 0, navigationTurnClearanceCm = 0, navigationSpeed = 0;
unsigned long navigationPollMs = 0, navigationSettleMs = 0;
unsigned long navigationStartMs = 0, navigationDurationMs = 0, navigationWaitMs = 0;
unsigned long lastRangeMs = 0, navigationRetryStartMs = 0, navigationRetryMs = 0;
uint8_t navigationGoodReadsRequired = 0, navigationGoodReads = 0;
bool navigationRecoveryClear = false;
char navigationDurationText[MAX_LINE + 1];

void encoderLeftISR() {
  if (digitalRead(leftMotor.getPortB()) == LOW) leftMotor.pulsePosMinus();
  else leftMotor.pulsePosPlus();
}

void encoderRightISR() {
  if (digitalRead(rightMotor.getPortB()) == LOW) rightMotor.pulsePosMinus();
  else rightMotor.pulsePosPlus();
}

void snapshotEncoders(long &left, long &right) {
  // AVR reads of the 32-bit ISR counters must be atomic.
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    left = leftMotor.getPulsePos();
    right = rightMotor.getPulsePos();
  }
}

void stopMotors() {
  // DIRECT_MODE prevents a later library loop from re-applying a PID/PWM target.
  leftMotor.setMotionMode(DIRECT_MODE);
  rightMotor.setMotionMode(DIRECT_MODE);
  leftMotor.setMotorPwm(0);
  rightMotor.setMotorPwm(0);
  moving = false;
  exploring = false;
  leftPwm = rightPwm = 0.0f;
}

void fail(const __FlashStringHelper *reason) {
  // Malformed input, busy commands and hardware faults all fail stopped.
  stopMotors();
  Serial.print(F("ERR "));
  Serial.println(reason);
}

void failStall(unsigned long now, long leftProgress, long rightProgress,
               bool leftStalled, bool rightStalled) {
  // Capture the last commanded PWM, then cut power BEFORE any serial output.
  const int pwmLeft = static_cast<int>(leftPwm) * leftDirection;
  const int pwmRight = static_cast<int>(rightPwm) * rightDirection;
  stopMotors();
  // Extend the existing ERR reason; old hosts already preserve the entire line.
  // Paired fields are SLOT1,SLOT2. Progress is direction-normalized pulse count.
  Serial.print(F("ERR stall slots="));
  if (leftStalled && rightStalled) Serial.print(F("1,2"));
  else Serial.print(leftStalled ? 1 : 2);
  Serial.print(F(" progress=")); Serial.print(leftProgress);
  Serial.print(','); Serial.print(rightProgress);
  Serial.print(F(" target=")); Serial.print(targetPulses);
  Serial.print(F(" pwm=")); Serial.print(pwmLeft);
  Serial.print(','); Serial.print(pwmRight);
  Serial.print(F(" idle_ms=")); Serial.print(now - leftProgressMs);
  Serial.print(','); Serial.print(now - rightProgressMs);
  Serial.print(F(" elapsed_ms=")); Serial.println(now - moveStartMs);
}

bool calibrationValid(float value) {
  return isfinite(value) && value >= MIN_CALIBRATION && value <= MAX_CALIBRATION;
}

uint32_t calibrationChecksum(float distance, float turn) {
  // Hash explicit float bytes; no dependence on struct padding.
  uint32_t result = 2166136261UL;
  const uint8_t *bytes = reinterpret_cast<const uint8_t *>(&distance);
  for (uint8_t i = 0; i < sizeof(float); ++i) result = (result ^ bytes[i]) * 16777619UL;
  bytes = reinterpret_cast<const uint8_t *>(&turn);
  for (uint8_t i = 0; i < sizeof(float); ++i) result = (result ^ bytes[i]) * 16777619UL;
  return result;
}

void loadCalibration() {
  CalibrationRecord saved;
  EEPROM.get(EEPROM.length() - sizeof(saved), saved);
  if (saved.magic == CAL_MAGIC && calibrationValid(saved.degPerCm) &&
      calibrationValid(saved.degPerTurnDeg) &&
      saved.checksum == calibrationChecksum(saved.degPerCm, saved.degPerTurnDeg)) {
    degPerCm = saved.degPerCm;
    degPerTurnDeg = saved.degPerTurnDeg;
  }
}

bool saveCalibration(float distance, float turn) {
  CalibrationRecord saved = {CAL_MAGIC, distance, turn, calibrationChecksum(distance, turn)};
  const int address = EEPROM.length() - sizeof(saved);
  EEPROM.put(address, saved);  // Update semantics reduce wear; only accepted while stopped.
  CalibrationRecord checked;
  EEPROM.get(address, checked);
  if (memcmp(&saved, &checked, sizeof(saved)) != 0) return false;
  degPerCm = distance;
  degPerTurnDeg = turn;
  return true;
}

bool parseNumber(const char *token, float &value) {
  // Decimal grammar only: strtod alone would also accept NaN/Inf/hex values.
  if (!token || !*token) return false;
  const char *cursor = token;
  if (*cursor == '+' || *cursor == '-') ++cursor;
  bool digits = false;
  while (isdigit(static_cast<unsigned char>(*cursor))) { digits = true; ++cursor; }
  if (*cursor == '.') {
    ++cursor;
    while (isdigit(static_cast<unsigned char>(*cursor))) { digits = true; ++cursor; }
  }
  if (!digits) return false;
  if (*cursor == 'e' || *cursor == 'E') {
    ++cursor;
    if (*cursor == '+' || *cursor == '-') ++cursor;
    const char *exponent = cursor;
    while (isdigit(static_cast<unsigned char>(*cursor))) ++cursor;
    if (cursor == exponent) return false;
  }
  if (*cursor) return false;
  char *end = NULL;
  value = strtod(token, &end);
  return end && !*end && isfinite(value);
}

bool parseColor(const char *token, uint8_t &value) {
  if (!token || !*token) return false;
  unsigned int parsed = 0;
  for (const char *cursor = token; *cursor; ++cursor) {
    if (*cursor < '0' || *cursor > '9') return false;
    parsed = parsed * 10 + (*cursor - '0');
    if (parsed > 255) return false;
  }
  value = parsed;
  return true;
}

void sendStatus() {
  long left, right;
  snapshotEncoders(left, right);
  Serial.print(F("STATUS batt="));
  Serial.print(analogRead(BATTERY_PIN) * (BATTERY_FULL_SCALE_V / 1024.0f), 2);
  Serial.print(F(" moving=")); Serial.print(moving ? 1 : 0);
  Serial.print(F(" enc=")); Serial.print(lroundf(left / PULSES_PER_WHEEL_DEG));
  Serial.print(','); Serial.println(lroundf(right / PULSES_PER_WHEEL_DEG));
}

void startMove(bool isTurn, float amount, float speed, const char *echo) {
  if (moving || exploring) { fail(F("busy")); return; }
  const float wheelDegrees = fabsf(amount) * (isTurn ? degPerTurnDeg : degPerCm);
  const long pulses = lroundf(wheelDegrees * PULSES_PER_WHEEL_DEG);
  if (amount != 0.0f && pulses < 1) { fail(F("resolution")); return; }
  stopMotors();
  turning = isTurn;
  strncpy(moveValue, echo, sizeof(moveValue) - 1);
  moveValue[sizeof(moveValue) - 1] = '\0';
  if (amount == 0.0f) {
    Serial.print(isTurn ? F("OK TURN ") : F("OK DRIVE "));
    Serial.println(moveValue);
    return;
  }
  snapshotEncoders(startLeft, startRight);
  previousLeft = startLeft; previousRight = startRight;
  targetPulses = pulses;
  leftDirection = amount > 0.0f ? 1 : -1;
  if (!isTurn) leftDirection *= DRIVE_FORWARD_POLARITY;
  // Counter-mounted tracks use opposite native directions for straight travel.
  rightDirection = isTurn ? leftDirection : -leftDirection;
  maxPwm = speed * (255.0f / 100.0f);
  moveStartMs = lastControlMs = leftProgressMs = rightProgressMs = millis();
  moving = true;
}

bool validRangePort(float port) {
  // Auriga's yellow/blue signal ports. Never probe its red motor-power ports.
  return port >= 6 && port <= 10 && port == floorf(port);
}

float readFrontCm(uint8_t port) {
  const uint8_t pin = mePort[port].s2;
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW); delayMicroseconds(2);
  digitalWrite(pin, HIGH); delayMicroseconds(10);
  digitalWrite(pin, LOW); pinMode(pin, INPUT);
  // The installed Makeblock measure(timeout) ignores its timeout argument.
  // Use the same single-wire trigger with an explicit, bounded echo timeout.
  const unsigned long echo = pulseIn(pin, HIGH, RANGE_TIMEOUT_US);
  const float cm = echo / 58.0f;
  return echo && cm >= 2.0f && cm <= 400.0f ? cm : -1.0f;
}

void navigationEvent(const __FlashStringHelper *state, float cm) {
  Serial.print(F("NAV ")); Serial.print(state); Serial.print(' '); Serial.println(cm, 2);
}

void navigationStep(bool turn) {
  // startMove normally rejects commands while exploration owns the motors.
  exploring = false;
  startMove(turn, turn ? navigationTurnDeg : navigationStepCm, navigationSpeed, "0");
  if (!moving) return;  // Invalid calibration/resolution already failed stopped.
  exploring = true;
  navigationPhase = turn ? NAV_TURN : NAV_FORWARD;
}

void updateNavigation() {
  if (!exploring) return;
  const unsigned long now = millis();
  if (now - lastCommandMs >= WATCHDOG_MS) { fail(F("watchdog")); return; }
  if (now - navigationStartMs >= navigationDurationMs) {
    stopMotors();
    Serial.print(F("OK EXPLORE ")); Serial.println(navigationDurationText);
    return;
  }
  if (navigationPhase == NAV_TURN) return;
  if (navigationPhase == NAV_RETRY) {
    if (now - navigationRetryStartMs >= navigationRetryMs) { fail(F("range_unknown")); return; }
    if (now - navigationRetryStartMs < navigationSettleMs) return;
  }
  if (navigationPhase == NAV_WAIT && now - navigationWaitMs < navigationSettleMs) return;
  if ((navigationPhase == NAV_FORWARD || navigationPhase == NAV_RETRY) &&
      now - lastRangeMs < navigationPollMs) return;
  const float cm = readFrontCm(rangePort);
  lastRangeMs = millis();
  if (navigationPhase == NAV_RETRY) {
    // A bounded echo wait can cross the deadline; never restart after it expires.
    if (lastRangeMs - navigationRetryStartMs >= navigationRetryMs) { fail(F("range_unknown")); return; }
    if (cm < 0) { navigationGoodReads = 0; return; }
    const bool clear = cm >= navigationClearCm;
    if (!navigationGoodReads || clear != navigationRecoveryClear) navigationGoodReads = 0;
    navigationRecoveryClear = clear;
    if (++navigationGoodReads < navigationGoodReadsRequired) return;
    navigationEvent(F("SENSOR_READY"), cm);
    navigationPhase = NAV_WAIT; // Already settled; decide using this fresh reading below.
  } else if (cm < 0) {
    // No coasting on a stale reading: cut power before reporting and retrying.
    stopMotors();
    exploring = true; navigationPhase = NAV_RETRY;
    navigationRetryStartMs = lastRangeMs; navigationGoodReads = 0;
    navigationEvent(F("SENSOR_WAIT"), -1.0f);
    return;
  }
  if (navigationPhase == NAV_FORWARD) {
    if (cm <= navigationStopCm) {
      stopMotors();
      exploring = true; navigationPhase = NAV_WAIT; navigationWaitMs = millis();
      navigationEvent(F("OBSTACLE"), cm);
    }
    return;
  }
  if (cm >= navigationClearCm) {
    navigationTurns = 0;
    navigationEvent(F("FORWARD"), cm);
    navigationStep(false);
  } else {
    if (cm < navigationTurnClearanceCm) { fail(F("obstacle_too_close")); return; }
    if (navigationTurns >= navigationMaxTurns) { fail(F("navigation_blocked")); return; }
    ++navigationTurns;
    navigationEvent(F("TURN"), cm);
    navigationStep(true);
  }
}

void handleLine(char *line) {
  // Fixed tokens and exact arity: no silent trailing arguments or numeric junk.
  char *tokens[12];
  uint8_t count = 0;
  char *context = NULL;
  for (char *token = strtok_r(line, " \t", &context); token;
       token = strtok_r(NULL, " \t", &context)) {
    if (count == 12) { fail(F("syntax")); return; }
    tokens[count++] = token;
  }
  if (!count) { fail(F("syntax")); return; }
  const char *command = tokens[0];
  if (strcmp(command, "STOP") == 0 && count == 1) {
    stopMotors();
    lastCommandMs = millis();
    Serial.println(F("OK STOP"));  // No interrupted DRIVE/TURN acknowledgement follows.
  } else if (strcmp(command, "HB") == 0 && count == 1) {
    lastCommandMs = millis();
  } else if (strcmp(command, "PING") == 0 && count == 1) {
    lastCommandMs = millis();
    Serial.println(F("PONG rover v1"));
  } else if (strcmp(command, "STATUS") == 0 && count == 1) {
    lastCommandMs = millis();
    sendStatus();
  } else if (strcmp(command, "RANGE") == 0 && count == 2) {
    float port;
    if (moving || exploring) { fail(F("busy")); return; }
    if (!parseNumber(tokens[1], port) || !validRangePort(port)) { fail(F("range_port")); return; }
    const float cm = readFrontCm(static_cast<uint8_t>(port));
    lastCommandMs = millis();
    Serial.print(F("RANGE port=")); Serial.print(static_cast<int>(port));
    Serial.print(F(" cm=")); Serial.print(cm, 2);
    Serial.print(F(" valid=")); Serial.println(cm >= 0 ? 1 : 0);
  } else if (strcmp(command, "NAVCFG") == 0 && count == 12) {
    if (moving || exploring) { fail(F("busy")); return; }
    float v[11];
    for (uint8_t i = 0; i < 11; ++i) {
      if (!parseNumber(tokens[i + 1], v[i])) { fail(F("navigation_config")); return; }
    }
    if (!validRangePort(v[0]) || v[1] < 20 || v[1] > 100 || v[2] < v[1] + 5 || v[2] > 200 ||
        v[3] < 1 || v[3] > 30 || v[4] < 15 || v[4] > 90 || v[5] < 1 || v[5] > 16 ||
        v[5] != floorf(v[5]) || v[6] < 60 || v[6] > 200 || v[6] != floorf(v[6]) ||
        v[7] < 100 || v[7] > 1000 || v[7] != floorf(v[7]) || v[8] < 5 || v[8] >= v[1] ||
        v[9] < 200 || v[9] > 5000 || v[9] != floorf(v[9]) ||
        v[10] < 2 || v[10] > 5 || v[10] != floorf(v[10]) ||
        v[9] <= v[7] + v[6] * v[10] + RANGE_TIMEOUT_US / 1000) {
      fail(F("navigation_config")); return;
    }
    rangePort = v[0]; navigationStopCm = v[1]; navigationClearCm = v[2];
    navigationStepCm = v[3]; navigationTurnDeg = v[4]; navigationMaxTurns = v[5];
    navigationPollMs = v[6]; navigationSettleMs = v[7]; navigationTurnClearanceCm = v[8];
    navigationRetryMs = v[9]; navigationGoodReadsRequired = v[10];
    navigationConfigured = true; lastCommandMs = millis();
    Serial.print(F("OK NAVCFG"));
    for (uint8_t i = 1; i < 12; ++i) { Serial.print(' '); Serial.print(tokens[i]); }
    Serial.println();
  } else if (strcmp(command, "EXPLORE") == 0 && count == 3) {
    float seconds, speed;
    if (moving || exploring) { fail(F("busy")); return; }
    if (!navigationConfigured) { fail(F("navigation_not_configured")); return; }
    if (!parseNumber(tokens[1], seconds) || !parseNumber(tokens[2], speed) ||
        seconds < 1 || seconds > 600 || speed < 1 || speed > MAX_SPEED_PCT) {
      fail(F("limit")); return;
    }
    stopMotors();
    strncpy(navigationDurationText, tokens[1], MAX_LINE); navigationDurationText[MAX_LINE] = 0;
    navigationSpeed = speed; navigationDurationMs = static_cast<unsigned long>(seconds * 1000);
    navigationStartMs = navigationWaitMs = lastCommandMs = millis();
    navigationTurns = 0; navigationPhase = NAV_WAIT; exploring = true;
  } else if ((strcmp(command, "DRIVE") == 0 || strcmp(command, "TURN") == 0) && count == 3) {
    float amount, speed;
    if (!parseNumber(tokens[1], amount) || !parseNumber(tokens[2], speed)) {
      fail(F("number")); return;
    }
    const bool isTurn = strcmp(command, "TURN") == 0;
    if (fabsf(amount) > (isTurn ? MAX_TURN_DEG : MAX_DRIVE_CM) ||
        speed < 1.0f || speed > MAX_SPEED_PCT) { fail(F("limit")); return; }
    lastCommandMs = millis();
    startMove(isTurn, amount, speed, tokens[1]);
  } else if (strcmp(command, "LED") == 0 && count == 4) {
    uint8_t r, g, b;
    if (!parseColor(tokens[1], r) || !parseColor(tokens[2], g) || !parseColor(tokens[3], b)) {
      fail(F("color")); return;
    }
    if (moving || exploring) { fail(F("busy")); return; }
    lastCommandMs = millis();
    ring.setColor(0, r, g, b); ring.show();
    Serial.print(F("OK LED ")); Serial.print(r); Serial.print(' ');
    Serial.print(g); Serial.print(' '); Serial.println(b);
  } else if (strcmp(command, "CAL") == 0 && count == 3) {
    float distance, turn;
    if (!parseNumber(tokens[1], distance) || !parseNumber(tokens[2], turn) ||
        !calibrationValid(distance) || !calibrationValid(turn)) { fail(F("calibration")); return; }
    if (moving || exploring) { fail(F("busy")); return; }
    lastCommandMs = millis();
    if (!saveCalibration(distance, turn)) { fail(F("eeprom")); return; }
    Serial.print(F("OK CAL ")); Serial.print(degPerCm, 6); Serial.print(' ');
    Serial.println(degPerTurnDeg, 6);
  } else {
    fail(F("syntax"));
  }
}

void pollSerial() {
  uint8_t budget = RX_BUDGET;
  while (budget-- && Serial.available()) {
    const int input = Serial.read();
    if (input == '\n') {
      if (!discardLine) {
        if (lineLength && lineBuffer[lineLength - 1] == '\r') --lineLength;
        lineBuffer[lineLength] = '\0';
        handleLine(lineBuffer);
      }
      lineLength = 0;
      discardLine = false;
    } else if (!discardLine) {
      if ((input < 32 && input != '\t' && input != '\r') || input > 126) {
        fail(F("ascii")); discardLine = true;
      } else if (lineLength == MAX_LINE) {
        fail(F("line_too_long")); discardLine = true;
      } else {
        lineBuffer[lineLength++] = static_cast<char>(input);
      }
    }
  }
}

float rampPwm(float current, float desired, float maxChange) {
  if (desired > current) return fminf(desired, current + maxChange);
  return fmaxf(desired, current - maxChange);
}

bool targetReached(long progress) {
  const long percentLimit = targetPulses / 100;
  const long tolerance = percentLimit < MAX_TARGET_ERROR_PULSES
      ? percentLimit : MAX_TARGET_ERROR_PULSES;
  return progress >= targetPulses - tolerance;
}

float desiredPwm(long progress) {
  if (exploring && navigationPhase == NAV_FORWARD) return maxPwm;
  if (targetReached(progress)) return 0.0f;
  // Straight-drive final slowdown caused measured stalls; retain requested power.
  // Exploration turns also retain demand, bounded by the same startup slew/ceiling.
  if (!turning || exploring) return maxPwm;
  // Reduce demand through the last quarter of the encoder distance. A bounded
  // friction floor avoids asymptotically approaching a target without reaching it.
  const float remaining = targetPulses - progress;
  const float rampFraction = fminf(1.0f, remaining * 4.0f / targetPulses);
  const float floorPwm = fminf(maxPwm, FRICTION_PWM);
  return floorPwm + (maxPwm - floorPwm) * rampFraction;
}

void updateMotion() {
  if (!moving) return;
  const unsigned long now = millis();
  if (now - lastCommandMs >= WATCHDOG_MS) { fail(F("watchdog")); return; }
  if (now - moveStartMs >= MAX_MOVE_MS) { fail(F("move_timeout")); return; }
  long left, right;
  snapshotEncoders(left, right);
  if (left != previousLeft) { previousLeft = left; leftProgressMs = now; }
  if (right != previousRight) { previousRight = right; rightProgressMs = now; }
  const long leftProgress = (left - startLeft) * leftDirection;
  const long rightProgress = (right - startRight) * rightDirection;
  if (leftProgress < -2 || rightProgress < -2) { fail(F("encoder_direction")); return; }
  const bool cruising = exploring && navigationPhase == NAV_FORWARD;
  if (cruising && targetReached(leftProgress) && targetReached(rightProgress)) {
    // Roll encoder checkpoints without stopping either track or resetting PWM.
    // Keep per-wheel last-progress times: checkpoint rollover cannot hide a stall.
    startLeft = left; startRight = right; moveStartMs = now;
  }
  const bool leftDone = !cruising && targetReached(leftProgress);
  const bool rightDone = !cruising && targetReached(rightProgress);
  // Cut output inside the bounded target tolerance, before checking for stalls.
  if (leftDone) { leftPwm = 0.0f; leftMotor.setMotorPwm(0); }
  if (rightDone) { rightPwm = 0.0f; rightMotor.setMotorPwm(0); }
  if (leftDone && rightDone) {
    if (exploring) {
      stopMotors();
      exploring = true; navigationPhase = NAV_WAIT; navigationWaitMs = millis();
      return;  // Internal segments never acknowledge the outer EXPLORE command.
    }
    stopMotors();
    Serial.print(turning ? F("OK TURN ") : F("OK DRIVE "));
    Serial.println(moveValue);
    return;
  }
  const bool leftStalled = !leftDone && now - leftProgressMs >= STALL_MS;
  const bool rightStalled = !rightDone && now - rightProgressMs >= STALL_MS;
  if (leftStalled || rightStalled) {
    failStall(now, leftProgress, rightProgress, leftStalled, rightStalled);
    return;
  }
  if (now - lastControlMs < CONTROL_MS) return;
  // Do not turn a delayed loop into a large, sudden acceleration step.
  const float change = PWM_SLEW_PER_S * CONTROL_MS / 1000.0f;
  lastControlMs = now;
  if (!leftDone) {
    leftPwm = rampPwm(leftPwm, desiredPwm(leftProgress), change);
    leftMotor.setMotorPwm(leftDirection * static_cast<int>(leftPwm));
  }
  if (!rightDone) {
    rightPwm = rampPwm(rightPwm, desiredPwm(rightProgress), change);
    rightMotor.setMotorPwm(rightDirection * static_cast<int>(rightPwm));
  }
}

void setup() {
  stopMotors();
  leftMotor.setPulse(ENCODER_PULSES_PER_REV);
  rightMotor.setPulse(ENCODER_PULSES_PER_REV);
  leftMotor.setRatio(MOTOR_GEAR_RATIO);
  rightMotor.setRatio(MOTOR_GEAR_RATIO);
  attachInterrupt(leftMotor.getIntNum(), encoderLeftISR, RISING);
  attachInterrupt(rightMotor.getIntNum(), encoderRightISR, RISING);
  // Official Auriga 8 kHz motor PWM setup; timer 0 (millis) is unchanged.
  TCCR1A = _BV(WGM10);
  TCCR1B = _BV(CS11) | _BV(WGM12);
  TCCR2A = _BV(WGM21) | _BV(WGM20);
  TCCR2B = _BV(CS21);
  ring.setpin(RING_PIN);
  ring.setColor(0, 0, 0, 0); ring.show();
  loadCalibration();
  Serial.begin(115200);
  lastCommandMs = millis();
  Serial.println(F("READY rover v1"));
}

void loop() {
  // Parse serial before motor updates: STOP cancels a pending completion reply.
  pollSerial();
  updateNavigation();
  updateMotion();
}
