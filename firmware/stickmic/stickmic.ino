// Heartbeat stick microphone: ESP32-PICO-D4 M5StickC Plus SE, Arduino-ESP32 3.x.
// Requires M5Unified/M5GFX 0.3.0: buffer-release callbacks keep capture continuous.
#include <M5Unified.h>
#include <Preferences.h>
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

// Wire/hardware constants; host gain and detector tuning live in profiles/*.yaml.
static constexpr uint32_t SERIAL_BAUD = 500000;  // CH552 bridge does not support 921600.
static constexpr uint32_t SAMPLE_RATE = 16000;
static constexpr size_t SAMPLE_COUNT = 256;
static constexpr size_t FRAME_BYTES = 2 + 2 + 2 + SAMPLE_COUNT * 2 + 2;
static constexpr uint8_t DEFAULT_MAG = 16;  // M5Unified's default multiplier.
static constexpr size_t TX_QUEUE_FRAMES = 8;  // 128 ms, bounded; overflow skips seq.
static constexpr size_t STATUS_TEXT_BYTES = 40;
static constexpr size_t COMMAND_BYTES = 128;
static constexpr size_t RX_BYTES_PER_LOOP = 64;  // A noisy sender cannot starve TX.
static constexpr uint32_t DISPLAY_PERIOD_MS = 100;  // At most 10 Hz.
static constexpr uint32_t BATTERY_PERIOD_MS = 1000;
static constexpr uint8_t BACKLIGHT = 96;  // 0..255; button A toggles to/from zero.
static_assert(FRAME_BYTES == 520, "stickmic v1 frame size");

struct AudioPacket {
  uint16_t seq;
  int16_t samples[SAMPLE_COUNT];
};
enum class Status : uint8_t { NO_SIGNS, POSSIBLE, CONFIRMED };
struct DisplayState {
  Status status = Status::NO_SIGNS;
  char text[STATUS_TEXT_BYTES + 1] = "Awaiting host";
};

static int16_t captureBuffers[2][SAMPLE_COUNT];
static QueueHandle_t txQueue = nullptr;
static Preferences preferences;
static bool preferencesReady = false;
static bool protocolStreaming = false;  // Only the Arduino loop owns the UART.
static std::atomic<bool> captureEnabled{false};
static std::atomic<bool> micFault{false};
static std::atomic<bool> displayStreaming{false};
static std::atomic<uint16_t> levelPeak{0};
static std::atomic<uint32_t> droppedFrames{0};
static std::atomic<uint8_t> magnification{DEFAULT_MAG};
static uint16_t nextSequence = 0;  // Only the mic callback advances this.
static DisplayState displayState;
static portMUX_TYPE displayMux = portMUX_INITIALIZER_UNLOCKED;

static uint16_t crc16(const uint8_t* data, size_t length) {
  uint16_t crc = 0xFFFF;  // CRC-16/CCITT-FALSE: no reflection, xorout=0.
  while (length--) {
    crc ^= static_cast<uint16_t>(*data++) << 8;
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                           : static_cast<uint16_t>(crc << 1);
    }
  }
  return crc;
}

static void put16(uint8_t* dst, uint16_t value) {
  dst[0] = value & 0xFF;
  dst[1] = value >> 8;
}

// Called by M5Unified's capture task after ownership of this buffer is released.
// No UART, LCD, flash writes, allocation, or blocking queue sends here. The other
// buffer is already queued. Requeue this one before the other buffer completes.
static void bufferReleased(void*, void* data, size_t length) {
  if (!captureEnabled.load()) return;
  if (length != SAMPLE_COUNT) {
    micFault.store(true);
    captureEnabled.store(false);
    return;
  }
  AudioPacket packet;
  packet.seq = nextSequence++;
  memcpy(packet.samples, data, sizeof(packet.samples));
  uint16_t peak = 0;
  for (const int16_t sample : packet.samples) {
    const uint16_t amplitude = sample < 0 ? -static_cast<int32_t>(sample) : sample;
    if (amplitude > peak) peak = amplitude;
  }
  // Retain transients between display updates, rather than only the last hop.
  uint16_t oldPeak = levelPeak.load();
  while (oldPeak < peak && !levelPeak.compare_exchange_weak(oldPeak, peak)) {}
  if (xQueueSend(txQueue, &packet, 0) != pdTRUE) ++droppedFrames;
  if (captureEnabled.load() &&
      !M5.Mic.record(static_cast<int16_t*>(data), SAMPLE_COUNT)) {
    // end() must be called from the loop, never from the capture callback.
    micFault.store(true);
    captureEnabled.store(false);
  }
}

static void stopCapture() {
  captureEnabled.store(false);
  M5.Mic.end();  // Joins capture before a buffer/queue is reused or MAG changes.
  if (txQueue) xQueueReset(txQueue);
  displayStreaming.store(false);
  levelPeak.store(0);
}

static bool startCapture() {
  if (!txQueue) return false;
  stopCapture();
  nextSequence = 0;
  droppedFrames.store(0);
  micFault.store(false);
  if (!M5.Mic.begin()) {
    micFault.store(true);
    return false;
  }
  captureEnabled.store(true);
  if (!M5.Mic.record(captureBuffers[0], SAMPLE_COUNT) ||
      !M5.Mic.record(captureBuffers[1], SAMPLE_COUNT)) {
    stopCapture();
    micFault.store(true);
    return false;
  }
  displayStreaming.store(true);
  return true;
}

static void sendFrame(const AudioPacket& packet) {
  uint8_t frame[FRAME_BYTES];
  frame[0] = 0xA5;
  frame[1] = 0x5A;
  put16(frame + 2, packet.seq);
  put16(frame + 4, SAMPLE_COUNT);
  for (size_t i = 0; i < SAMPLE_COUNT; ++i) {
    put16(frame + 6 + 2 * i, static_cast<uint16_t>(packet.samples[i]));
  }
  put16(frame + FRAME_BYTES - 2, crc16(frame + 2, FRAME_BYTES - 4));
  // UART may wait for TX space; the independent mic task keeps filling buffers.
  // Send a whole frame before examining STOP, so text never splits a frame.
  Serial.write(frame, sizeof(frame));
}

static char* trim(char* text) {
  while (*text == ' ') ++text;
  size_t length = strlen(text);
  while (length && text[length - 1] == ' ') text[--length] = '\0';
  return text;
}

static bool setStatus(char* args) {
  char* text = strchr(args, ' ');
  if (text) *text++ = '\0';
  Status status;
  if (!strcmp(args, "NO_SIGNS")) status = Status::NO_SIGNS;
  else if (!strcmp(args, "POSSIBLE")) status = Status::POSSIBLE;
  else if (!strcmp(args, "CONFIRMED")) status = Status::CONFIRMED;
  else return false;
  if (text) text = trim(text);
  portENTER_CRITICAL(&displayMux);
  displayState.status = status;
  strncpy(displayState.text, text ? text : "", STATUS_TEXT_BYTES);
  displayState.text[STATUS_TEXT_BYTES] = '\0';
  portEXIT_CRITICAL(&displayMux);
  return true;
}

static void handleCommand(char* line) {
  char* command = trim(line);
  if (!*command) return;
  char* args = strchr(command, ' ');
  if (args) {
    *args++ = '\0';
    args = trim(args);
  } else {
    args = command + strlen(command);
  }
  if (!strcmp(command, "STOP") && !*args) {
    stopCapture();
    protocolStreaming = false;
    return;  // No ACK; a subsequent PING confirms return to text mode.
  }
  if (!strcmp(command, "STATUS")) {
    if (!setStatus(args) && !protocolStreaming) Serial.println("ERR status");
    return;  // No status ACK in either mode.
  }
  if (protocolStreaming) return;  // Absolutely no text within the binary stream.
  if (!strcmp(command, "PING") && !*args) {
    Serial.println("PONG stickmic v1");
  } else if (!strcmp(command, "START") && !*args) {
    if (startCapture()) protocolStreaming = true;  // First response is frame 0.
    else Serial.println("ERR mic_start");
  } else if (!strcmp(command, "MAG")) {
    char* end;
    const long value = strtol(args, &end, 10);
    if (!*args || *end || value < 1 || value > 255) {
      Serial.println("ERR mag_range_1_255");
    } else if (!preferencesReady ||
               preferences.putUChar("mag", static_cast<uint8_t>(value)) != 1) {
      Serial.println("ERR mag_save");
    } else {
      auto cfg = M5.Mic.config();  // Only changed while capture is stopped.
      cfg.magnification = static_cast<uint8_t>(value);
      M5.Mic.config(cfg);
      magnification.store(cfg.magnification);
      Serial.printf("OK MAG %u\n", cfg.magnification);
    }
  } else {
    Serial.println("ERR command");  // Includes K, CAL, TEST, and MODE PIEZO.
  }
}

static void readCommands() {
  static char line[COMMAND_BYTES];
  static size_t used = 0;
  static bool invalid = false;
  for (size_t count = 0; count < RX_BYTES_PER_LOOP && Serial.available(); ++count) {
    const int value = Serial.read();
    if (value < 0) break;
    if (value == '\r') continue;  // Accept LF and CRLF.
    if (value == '\n') {
      line[used] = '\0';
      if (invalid) {
        if (!protocolStreaming) Serial.println("ERR line");
      } else {
        handleCommand(line);
      }
      used = 0;
      invalid = false;
    } else if (value < 0x20 || value > 0x7E || used == sizeof(line) - 1) {
      invalid = true;  // Discard the entire malformed line through its newline.
    } else if (!invalid) {
      line[used++] = static_cast<char>(value);
    }
  }
}

static void displayTask(void*) {
  bool backlight = true;
  bool first = true;
  DisplayState previous;
  uint32_t lastDraw = 0;
  uint32_t lastBattery = 0;
  int16_t batteryMv = -1;
  for (;;) {
    M5.update();  // Only this task touches the buttons and LCD after setup.
    if (M5.BtnA.wasClicked()) {
      backlight = !backlight;
      M5.Display.setBrightness(backlight ? BACKLIGHT : 0);
      first = true;
    }
    const uint32_t now = millis();
    if (backlight && (first || now - lastDraw >= DISPLAY_PERIOD_MS)) {
      lastDraw = now;
      DisplayState state;
      portENTER_CRITICAL(&displayMux);
      state = displayState;
      portEXIT_CRITICAL(&displayMux);
      const uint16_t bg = state.status == Status::CONFIRMED ? 0xB800
                          : state.status == Status::POSSIBLE ? 0xFD20 : 0x632C;
      const uint16_t fg = state.status == Status::POSSIBLE ? 0x0000 : 0xFFFF;
      if (first || state.status != previous.status) {
        M5.Display.fillScreen(bg);
        M5.Display.setTextColor(fg, bg);
        M5.Display.setTextSize(1);
        M5.Display.setCursor(8, 6);
        M5.Display.print("HEARTBEAT / STICK MIC");
        M5.Display.setTextSize(3);
        M5.Display.setCursor(8, 23);
        M5.Display.print(state.status == Status::CONFIRMED ? "CONFIRMED"
                         : state.status == Status::POSSIBLE ? "POSSIBLE" : "NO SIGNS");
      }
      M5.Display.setTextSize(1);
      if (first || state.status != previous.status || strcmp(state.text, previous.text)) {
        M5.Display.fillRect(0, 54, M5.Display.width(), 24, bg);
        // 36 characters fit with margins on the 240-pixel landscape display.
        char firstLine[37];
        strncpy(firstLine, state.text, 36);
        firstLine[36] = '\0';
        M5.Display.setCursor(8, 55);
        M5.Display.print(firstLine);
        if (strlen(state.text) > 36) {
          M5.Display.setCursor(8, 66);
          M5.Display.print(state.text + 36);
        }
      }
      const int barWidth = M5.Display.width() - 16;
      const int filled = static_cast<uint32_t>(levelPeak.exchange(0)) * (barWidth - 2) / 32768;
      M5.Display.drawRect(8, 85, barWidth, 11, fg);
      M5.Display.fillRect(9, 86, barWidth - 2, 9, bg);
      if (filled) M5.Display.fillRect(9, 86, filled, 9, fg);
      if (first || now - lastBattery >= BATTERY_PERIOD_MS) {
        batteryMv = M5.Power.getBatteryVoltage();  // M5Unified returns millivolts.
        lastBattery = now;
      }
      M5.Display.fillRect(0, 104, M5.Display.width(), 30, bg);
      M5.Display.setCursor(8, 104);
      const bool active = displayStreaming.load();
      M5.Display.printf("%s  MAG %u", active ? ((now / 500) % 2 ? "* STREAM" : "  STREAM") : "IDLE",
                        static_cast<unsigned>(magnification.load()));
      M5.Display.setCursor(M5.Display.width() - 44, 104);
      if (batteryMv > 0) M5.Display.printf("%.2fV", batteryMv / 1000.0f);
      else M5.Display.print("--.--V");
      M5.Display.setCursor(8, 119);
      if (micFault.load()) M5.Display.print("MIC ERROR: STOP then START");
      else if (!preferencesReady) M5.Display.print("MAG storage unavailable");
      else M5.Display.printf("TX dropped: %lu", static_cast<unsigned long>(droppedFrames.load()));
      previous = state;
      first = false;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

void setup() {
  // Never let SDK/library diagnostics corrupt binary PCM.
  esp_log_level_set("*", ESP_LOG_NONE);
  M5.Log.setLogLevel(m5::log_target_serial, ESP_LOG_NONE);
  M5.Log.setLogLevel(m5::log_target_display, ESP_LOG_NONE);
  auto cfg = M5.config();
  cfg.serial_baudrate = 0;
  cfg.internal_imu = false;  // Plus SE has no IMU; do not initialize one.
  cfg.external_imu = false;
  cfg.internal_rtc = false;
  cfg.internal_spk = false;
  cfg.internal_mic = true;
  cfg.external_display_value = 0;
  cfg.fallback_board = m5::board_t::board_M5StickCPlus;
  // This is a known Plus SE, so do not depend on IMU-based board discovery.
  m5gfx::detect_config_t displayConfig;
  displayConfig.fixed_board = m5::board_t::board_M5StickCPlus;
  M5.Display.setDetectConfig(displayConfig);
  M5.begin(cfg);
  M5.Speaker.end();
  M5.Mic.end();
  if (M5.Display.width() < M5.Display.height()) {
    M5.Display.setRotation(M5.Display.getRotation() ^ 1);
  }
  M5.Display.setBrightness(BACKLIGHT);
  M5.Display.setTextWrap(false);
  Serial.setDebugOutput(false);
  Serial.begin(SERIAL_BAUD);
  preferencesReady = preferences.begin("hb-stickmic", false);
  uint8_t mag = preferencesReady ? preferences.getUChar("mag", DEFAULT_MAG) : DEFAULT_MAG;
  if (!mag) mag = DEFAULT_MAG;
  magnification.store(mag);
  auto micConfig = M5.Mic.config();
  micConfig.sample_rate = SAMPLE_RATE;
  micConfig.stereo = false;
  micConfig.magnification = mag;
  micConfig.task_priority = 4;
  micConfig.task_pinned_core = 0;
  M5.Mic.config(micConfig);
  M5.Mic.setBufferReleaseCallback(nullptr, bufferReleased);
  txQueue = xQueueCreate(TX_QUEUE_FRAMES, sizeof(AudioPacket));
  if (!txQueue) micFault.store(true);
  const BaseType_t uiStarted = xTaskCreatePinnedToCore(displayTask, "stickmic-ui", 4096,
                                                     nullptr, 1, nullptr, 1);
  Serial.println("READY stickmic v1 rate=16000");
  if (uiStarted != pdPASS) Serial.println("ERR display_task");
}

void loop() {
  readCommands();  // No blocking readString/readBytes timeouts.
  if (micFault.load() && M5.Mic.isRunning()) stopCapture();
  AudioPacket packet;
  if (protocolStreaming && txQueue && xQueueReceive(txQueue, &packet, 0) == pdTRUE) {
    sendFrame(packet);
  } else {
    delay(1);  // Yield when idle; capture and LCD run independently.
  }
}
