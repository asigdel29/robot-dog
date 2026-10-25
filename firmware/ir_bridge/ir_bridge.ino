// ir_bridge.ino: firmware for the IR bridge between robotd on the Pi and the toy dog.
//
// Board: Adafruit ESP32 Feather V2. IR module: Adafruit IR transceiver, IRin on
// GPIO33 (receiver output), IRout on GPIO27 (emitter driver input). robotd talks to
// the board over USB serial at 115200 baud, one JSON object per line. The wire
// protocol is docs/design/protocol.md section 2; sim/fake_esp32.py is the reference
// behavior and tests/test_conformance.py checks both. The bridge owns the hard
// time limits (lease deadman, heartbeat watchdog), so a stalled Pi cannot keep the
// dog moving. robotd sends the IR registry to RAM at each handshake, so a new
// capture never needs a new flash. The firmware never starts WiFi or Bluetooth.

#include <ArduinoJson.h>

static const char FW_VERSION[] = "ir_bridge-1";  ///< reported in hello, so robotd logs which build runs
static const size_t LINE_LIMIT = 1024;  ///< longest line in either direction; longer input lines are discarded

/** Reported bridge states from protocol.md. */
enum State : uint8_t { DISARMED, READY, EXECUTING, FAULT };
static const char *const STATE_NAMES[] = {"DISARMED", "READY", "EXECUTING", "FAULT"};

static State state = DISARMED;     ///< start-up never moves the dog
static bool haveSession = false;   ///< false until the first hello; ops before it are refused
static int64_t lastId = 0;         ///< ids must grow within a session, so a replayed op is refused
static uint32_t bootId = 0;        ///< random at boot, so robotd can tell a reset
static uint32_t maxLoopUs = 0;     ///< worst loop pass since boot, reported in state
static char lineBuf[LINE_LIMIT + 1];  ///< the serial line being received
static size_t lineLen = 0;         ///< used part of lineBuf
static bool lineTooLong = false;   ///< discard input until the next newline

/** Write one event line. One buffer fits the largest event, a cap frame of MAX_RAW durations. */
static void emit(const JsonDocument &event) {
  static char out[LINE_LIMIT + 1];
  size_t n = serializeJson(event, out, LINE_LIMIT);
  out[n] = '\n';
  Serial.write(out, n + 1);
}

/** Refuse an op; the id is echoed as received, because a refused id may not be an integer. */
static void nack(JsonVariantConst id, const char *why) {
  JsonDocument event;
  event["ev"] = "nack";
  event["id"] = id;
  event["why"] = why;
  emit(event);
}

/** Report the session and the boot; robotd matches sid to its own hello, and sid 0 marks a reset. */
static void emitHello(int64_t sid) {
  JsonDocument event;
  event["ev"] = "hello";
  event["sid"] = sid;
  event["boot"] = bootId;
  event["fw"] = FW_VERSION;
  event["state"] = STATE_NAMES[state];
  emit(event);
}

/** hello: start a new session. The bridge forgets entries, ids, the hold and the capture, without done. */
static void opHello(JsonObjectConst in) {
  lastId = 0;
  state = DISARMED;
  haveSession = true;
  emitHello(in["sid"] | 0LL);
}

/** hb: feed the watchdog and report the state. */
static void opHb(JsonObjectConst in) {
  JsonDocument event;
  event["ev"] = "state";
  event["s"] = STATE_NAMES[state];
  event["max_loop_us"] = maxLoopUs;
  event["seq"] = in["seq"];
  emit(event);
}

/** Apply one line with the rules of the fake: session check, then id check, then the op. */
static void handleLine(const char *line, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, line, len) != DeserializationError::Ok || !doc.is<JsonObjectConst>()) {
    return;
  }
  JsonObjectConst in = doc.as<JsonObjectConst>();
  const char *op = in["op"] | "";
  if (strcmp(op, "hello") == 0) {
    opHello(in);
    return;
  }
  if (strcmp(op, "hb") == 0) {
    opHb(in);
    return;
  }
  JsonVariantConst idField = in["id"];
  if (!haveSession) {
    nack(idField, "no hello");
    return;
  }
  if (!idField.is<int64_t>() || idField.as<int64_t>() <= lastId) {
    nack(idField, "old id");
    return;
  }
  int64_t id = idField.as<int64_t>();
  lastId = id;
}

/** Read serial input and handle at most one line, so one pass never runs more than one op.
    A waiting send holds back later ops, so a disarm cannot overtake it. */
static void readSerial() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c != '\n') {
      if (lineLen < LINE_LIMIT) {
        lineBuf[lineLen++] = c;
      } else {
        lineTooLong = true;  // discard the rest of this line
      }
      continue;
    }
    size_t len = lineLen;
    bool tooLong = lineTooLong;
    lineLen = 0;
    lineTooLong = false;
    if (!tooLong) {
      handleLine(lineBuf, len);
      return;
    }
  }
}

/** Boot: emitter off, serial up, then one hello with sid 0. Start-up never transmits. */
void setup() {
  Serial.begin(115200);
  bootId = esp_random() >> 1;  // differs at each reset, so robotd can see one
  emitHello(0);
}

/** One pass: watchdog, one op, the send, hold frames, capture frames. max_loop_us is the worst pass. */
void loop() {
  uint32_t start = micros();
  readSerial();
  uint32_t took = micros() - start;
  if (took > maxLoopUs) {
    maxLoopUs = took;
  }
}
