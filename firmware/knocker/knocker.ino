#include <Arduino.h>
#include <ctype.h>
#include <stdint.h>
#include <string.h>

#if defined(ESP32)
#include <ESP32Servo.h>
#include <Preferences.h>
#else
#include <EEPROM.h>
#include <Servo.h>
#endif

// AVR inputs have no internal pull-down. Enable only with a Grove button or
// a 10k pull-down on D2 so a missing optional button cannot trigger strikes.
#ifndef KNOCKER_ENABLE_BUTTON
#if defined(ESP32)
#define KNOCKER_ENABLE_BUTTON 1
#else
#define KNOCKER_ENABLE_BUTTON 0
#endif
#endif

namespace {
#if defined(ESP32)
const uint8_t SERVO_PIN = 13;
const uint8_t LED_PIN = 2;
const uint8_t BUTTON_PIN = 0;
const uint8_t BUTTON_MODE = INPUT_PULLUP;
const uint8_t BUTTON_PRESSED_LEVEL = LOW;
Preferences preferences;
bool storageAvailable = false;
#else
const uint8_t SERVO_PIN = 5;
const uint8_t LED_PIN = 4;
const uint8_t BUTTON_PIN = 2;
const uint8_t BUTTON_MODE = INPUT;
const uint8_t BUTTON_PRESSED_LEVEL = HIGH;
#endif

// P02 owns only firmware and supplies these hardware defaults and limits.
const unsigned long SERIAL_BAUD = 115200;
const uint16_t DEFAULT_REST_DEG = 60;
const uint16_t DEFAULT_STRIKE_DEG = 95;
const uint16_t DEFAULT_HOLD_MS = 70;
const uint16_t MIN_ANGLE_DEG = 0;
const uint16_t MAX_ANGLE_DEG = 180;
const uint16_t MIN_HOLD_MS = 30;
const uint16_t MAX_HOLD_MS = 200;
const uint16_t MIN_KNOCKS = 1;
const uint16_t MAX_KNOCKS = 9;
const uint16_t MIN_INTERVAL_MS = 250;
const uint16_t MAX_INTERVAL_MS = 2000;
const uint16_t TEST_KNOCKS = 3;
const uint16_t TEST_INTERVAL_MS = 450;
const uint16_t DETACH_SETTLE_MS = 150;
const unsigned long BUTTON_DEBOUNCE_MS = 30;
const size_t LINE_CAPACITY = 80;
const uint32_t STORAGE_MAGIC = 0x48424b31UL;  // "HBK1"
const uint16_t STORAGE_VERSION = 1;
const int EEPROM_ADDRESS = 0;

struct Calibration {
  uint16_t rest;
  uint16_t strike;
  uint16_t hold;
};

struct StoredCalibration {
  uint32_t magic;
  uint16_t version;
  uint16_t rest;
  uint16_t strike;
  uint16_t hold;
  uint16_t checksum;
};

// Explicit declarations also keep Arduino's generated prototypes from putting
// custom types ahead of their declarations.
bool validCalibration(const Calibration &value);
uint16_t recordChecksum(const StoredCalibration &record);
bool validRecord(const StoredCalibration &record);
StoredCalibration makeRecord(const Calibration &value);
void loadCalibration();
bool saveCalibration(const Calibration &value);
void printError(const char *reason);
bool commandIs(const char *actual, const char *expected);
bool parseNumber(const char *token, uint16_t minimum, uint16_t maximum,
                 uint16_t &value);
bool runPattern(uint16_t knocks, uint16_t intervalMs);
void processLine(char *line);
void readSerial();
#if KNOCKER_ENABLE_BUTTON
bool isButtonPressed();
void pollButton();
#endif

Calibration calibration = {
    DEFAULT_REST_DEG, DEFAULT_STRIKE_DEG, DEFAULT_HOLD_MS};
Servo hammer;
char lineBuffer[LINE_CAPACITY];
size_t lineLength = 0;
const char *lineError = nullptr;
bool lastWasCR = false;
#if KNOCKER_ENABLE_BUTTON
bool buttonRaw = false;
bool buttonStable = false;
bool buttonArmed = false;
unsigned long buttonChangedAt = 0;
#endif

bool validCalibration(const Calibration &value) {
  return value.rest <= MAX_ANGLE_DEG && value.strike <= MAX_ANGLE_DEG &&
         value.hold >= MIN_HOLD_MS && value.hold <= MAX_HOLD_MS;
}

uint16_t recordChecksum(const StoredCalibration &record) {
  // Hash explicit fields, never padding bytes (AVR and ESP32 align differently).
  const uint16_t fields[] = {
      static_cast<uint16_t>(record.magic),
      static_cast<uint16_t>(record.magic >> 16), record.version,
      record.rest, record.strike, record.hold};
  uint16_t checksum = 0x811c;
  for (size_t i = 0; i < sizeof(fields) / sizeof(fields[0]); ++i) {
    checksum = static_cast<uint16_t>((checksum ^ (fields[i] & 0xff)) * 257UL);
    checksum = static_cast<uint16_t>((checksum ^ (fields[i] >> 8)) * 257UL);
  }
  return checksum;
}

bool validRecord(const StoredCalibration &record) {
  const Calibration value = {record.rest, record.strike, record.hold};
  return record.magic == STORAGE_MAGIC && record.version == STORAGE_VERSION &&
         record.checksum == recordChecksum(record) && validCalibration(value);
}

StoredCalibration makeRecord(const Calibration &value) {
  StoredCalibration record = {};
  record.magic = STORAGE_MAGIC;
  record.version = STORAGE_VERSION;
  record.rest = value.rest;
  record.strike = value.strike;
  record.hold = value.hold;
  record.checksum = recordChecksum(record);
  return record;
}

void loadCalibration() {
  StoredCalibration record = {};
#if defined(ESP32)
  storageAvailable = preferences.begin("hb-knocker", false);
  if (!storageAvailable ||
      preferences.getBytesLength("cal") != sizeof(record) ||
      preferences.getBytes("cal", &record, sizeof(record)) != sizeof(record)) {
    return;
  }
#else
  if (EEPROM.length() < EEPROM_ADDRESS + static_cast<int>(sizeof(record))) {
    return;
  }
  EEPROM.get(EEPROM_ADDRESS, record);
#endif
  // Erased, partial, corrupted and out-of-range records all use safe defaults.
  if (validRecord(record)) {
    calibration.rest = record.rest;
    calibration.strike = record.strike;
    calibration.hold = record.hold;
  }
}

bool saveCalibration(const Calibration &value) {
  const StoredCalibration record = makeRecord(value);
  StoredCalibration saved = {};
#if defined(ESP32)
  if (!storageAvailable ||
      preferences.putBytes("cal", &record, sizeof(record)) != sizeof(record) ||
      preferences.getBytesLength("cal") != sizeof(saved) ||
      preferences.getBytes("cal", &saved, sizeof(saved)) != sizeof(saved)) {
    return false;
  }
#else
  if (EEPROM.length() < EEPROM_ADDRESS + static_cast<int>(sizeof(record))) {
    return false;
  }
  EEPROM.put(EEPROM_ADDRESS, record);
  EEPROM.get(EEPROM_ADDRESS, saved);
#endif
  return validRecord(saved) && saved.rest == value.rest &&
         saved.strike == value.strike && saved.hold == value.hold;
}

void printError(const char *reason) {
  Serial.print(F("ERR "));
  Serial.println(reason);
}

bool commandIs(const char *actual, const char *expected) {
  while (*actual && *expected) {
    if (toupper(static_cast<unsigned char>(*actual)) != *expected) {
      return false;
    }
    ++actual;
    ++expected;
  }
  return *actual == '\0' && *expected == '\0';
}

bool parseNumber(const char *token, uint16_t minimum, uint16_t maximum,
                 uint16_t &value) {
  if (*token == '\0') {
    return false;
  }
  uint16_t parsed = 0;
  for (const char *p = token; *p; ++p) {
    if (*p < '0' || *p > '9') {
      return false;
    }
    const uint8_t digit = static_cast<uint8_t>(*p - '0');
    // Reject before arithmetic can wrap, including extremely long decimals.
    if (parsed > maximum / 10 ||
        (parsed == maximum / 10 && digit > maximum % 10)) {
      return false;
    }
    parsed = static_cast<uint16_t>(parsed * 10 + digit);
  }
  if (parsed < minimum) {
    return false;
  }
  value = parsed;
  return true;
}

bool runPattern(uint16_t knocks, uint16_t intervalMs) {
  for (uint16_t i = 0; i < knocks; ++i) {
    hammer.attach(SERVO_PIN);
    if (!hammer.attached()) {
      digitalWrite(LED_PIN, LOW);
      hammer.detach();
      printError("servo attach");
      return false;
    }
    digitalWrite(LED_PIN, HIGH);
    hammer.write(calibration.strike);
    delay(calibration.hold);
    hammer.write(calibration.rest);
    digitalWrite(LED_PIN, LOW);
    delay(intervalMs - calibration.hold);
  }
  delay(DETACH_SETTLE_MS);
  hammer.detach();
  return true;
}

void processLine(char *line) {
  char *tokens[5];
  size_t count = 0;
  char *cursor = line;
  while (*cursor) {
    while (isspace(static_cast<unsigned char>(*cursor))) {
      ++cursor;
    }
    if (!*cursor) {
      break;
    }
    if (count == sizeof(tokens) / sizeof(tokens[0])) {
      printError("too many arguments");
      return;
    }
    tokens[count++] = cursor;
    while (*cursor && !isspace(static_cast<unsigned char>(*cursor))) {
      ++cursor;
    }
    if (*cursor) {
      *cursor++ = '\0';
    }
  }
  if (count == 0) {
    printError("empty command");
    return;
  }

  if (commandIs(tokens[0], "PING")) {
    if (count != 1) {
      printError("usage PING");
      return;
    }
    Serial.println(F("PONG knocker v1"));
  } else if (commandIs(tokens[0], "K")) {
    uint16_t knocks = 0;
    uint16_t intervalMs = 0;
    if (count != 3) {
      printError("usage K n interval_ms");
      return;
    }
    if (!parseNumber(tokens[1], MIN_KNOCKS, MAX_KNOCKS, knocks)) {
      printError("n must be 1..9");
      return;
    }
    if (!parseNumber(tokens[2], MIN_INTERVAL_MS, MAX_INTERVAL_MS, intervalMs)) {
      printError("interval_ms must be 250..2000");
      return;
    }
    if (runPattern(knocks, intervalMs)) {
      Serial.print(F("OK K "));
      Serial.println(knocks);
    }
  } else if (commandIs(tokens[0], "TEST")) {
    if (count != 1) {
      printError("usage TEST");
      return;
    }
    if (runPattern(TEST_KNOCKS, TEST_INTERVAL_MS)) {
      Serial.println(F("OK TEST"));
    }
  } else if (commandIs(tokens[0], "CAL")) {
    Calibration next = {};
    if (count != 4) {
      printError("usage CAL rest_deg strike_deg hold_ms");
      return;
    }
    if (!parseNumber(tokens[1], MIN_ANGLE_DEG, MAX_ANGLE_DEG, next.rest) ||
        !parseNumber(tokens[2], MIN_ANGLE_DEG, MAX_ANGLE_DEG, next.strike)) {
      printError("angles must be 0..180");
      return;
    }
    if (!parseNumber(tokens[3], MIN_HOLD_MS, MAX_HOLD_MS, next.hold)) {
      printError("hold_ms must be 30..200");
      return;
    }
    if (!saveCalibration(next)) {
      printError("storage");
      return;
    }
    calibration = next;
    Serial.print(F("OK CAL "));
    Serial.print(calibration.rest);
    Serial.print(' ');
    Serial.print(calibration.strike);
    Serial.print(' ');
    Serial.println(calibration.hold);
  } else {
    printError("unknown command");
  }
}

void readSerial() {
  while (Serial.available() > 0) {
    const int next = Serial.read();
    if (next < 0) {
      return;
    }
    const char ch = static_cast<char>(next);
    if (ch == '\r' || ch == '\n') {
      // CRLF is one terminator, while either CR or LF alone also works.
      if (ch == '\n' && lastWasCR) {
        lastWasCR = false;
        continue;
      }
      lastWasCR = ch == '\r';
      if (lineError) {
        printError(lineError);
      } else {
        lineBuffer[lineLength] = '\0';
        processLine(lineBuffer);
      }
      lineLength = 0;
      lineError = nullptr;
      continue;
    }
    lastWasCR = false;
    if (lineError) {
      continue;  // Never execute any prefix of a malformed/overlong command.
    }
    const unsigned char byte = static_cast<unsigned char>(ch);
    if (byte > 0x7e || (byte < 0x20 && !isspace(byte))) {
      lineError = "invalid character";
    } else if (lineLength >= LINE_CAPACITY - 1) {
      lineError = "line too long";
    } else {
      lineBuffer[lineLength++] = ch;
    }
  }
}

#if KNOCKER_ENABLE_BUTTON
bool isButtonPressed() {
  return digitalRead(BUTTON_PIN) == BUTTON_PRESSED_LEVEL;
}

void pollButton() {
  const bool pressed = isButtonPressed();
  const unsigned long now = millis();
  if (pressed != buttonRaw) {
    buttonRaw = pressed;
    buttonChangedAt = now;
  }
  if (buttonRaw == buttonStable || now - buttonChangedAt < BUTTON_DEBOUNCE_MS) {
    return;
  }
  buttonStable = buttonRaw;
  if (!buttonStable) {
    buttonArmed = true;
  } else if (buttonArmed) {
    buttonArmed = false;
    if (runPattern(TEST_KNOCKS, TEST_INTERVAL_MS)) {
      Serial.println(F("OK TEST"));
    }
  }
}
#endif
}  // namespace

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
#if KNOCKER_ENABLE_BUTTON
  pinMode(BUTTON_PIN, BUTTON_MODE);
  buttonRaw = buttonStable = isButtonPressed();
  buttonArmed = !buttonStable;
  buttonChangedAt = millis();
#endif
  Serial.begin(SERIAL_BAUD);
  loadCalibration();
  // No attach/write at boot or calibration: only validated patterns move it.
  Serial.print(F("READY knocker v1 rest="));
  Serial.print(calibration.rest);
  Serial.print(F(" strike="));
  Serial.print(calibration.strike);
  Serial.print(F(" hold="));
  Serial.println(calibration.hold);
}

void loop() {
  // Deliberately blocking: buffered commands run after the current pattern.
  readSerial();
#if KNOCKER_ENABLE_BUTTON
  pollButton();
#endif
}
