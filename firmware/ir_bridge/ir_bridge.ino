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
static const uint8_t MAX_ENTRIES = 24;  ///< RAM table size; the remote has 12 buttons
static const uint8_t MAX_RAW = 120;  ///< raw durations per entry, the same cap as robotd's registry rule
static const uint32_t MAX_FRAME_US = 150000;  ///< longest raw frame, so one frame bounds the longest loop pass
static const size_t NAME_LEN = 32;   ///< entry name buffer in bytes, with the terminating zero

/** Reported bridge states from protocol.md. */
enum State : uint8_t { DISARMED, READY, EXECUTING, FAULT };
static const char *const STATE_NAMES[] = {"DISARMED", "READY", "EXECUTING", "FAULT"};

/** How the remote repeats a held button: NEC repeat frame, the full frame again, or nothing. */
enum Repeat : uint8_t { REPEAT_NEC, REPEAT_RESEND, REPEAT_NONE };

/** One registry command in RAM: enough to send its first frame and its repeat frames. */
struct Entry {
  char name[NAME_LEN];     ///< registry name that send and hold refer to
  bool nec;                ///< true: encode as NEC; false: replay raw
  bool hasData;            ///< NEC only: send data as it was captured, byte for byte
  uint32_t data;           ///< the captured 32-bit NEC frame, as IRremote decodes it
  uint16_t addr;           ///< NEC address, 8 or 16 bit, when there is no data
  uint8_t cmd;             ///< NEC command, when there is no data
  uint8_t khz;             ///< carrier for raw replay
  Repeat repeat;           ///< what a held button sends after the first frame
  uint16_t periodMs;       ///< frame start to frame start while held
  uint8_t rawLen;          ///< used part of raw
  uint16_t raw[MAX_RAW];   ///< microseconds, mark first
};

static State state = DISARMED;     ///< start-up never moves the dog
static bool haveSession = false;   ///< false until the first hello; ops before it are refused
static int64_t lastId = 0;         ///< ids must grow within a session, so a replayed op is refused
static uint32_t bootId = 0;        ///< random at boot, so robotd can tell a reset
static Entry entries[MAX_ENTRIES]; ///< the registry, loaded by robotd at each hello
static uint8_t entryCount = 0;     ///< loaded part of entries; arm checks it
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

/** Emit an event that carries only an op id: ack, done. */
static void emitId(const char *ev, int64_t id) {
  JsonDocument event;
  event["ev"] = ev;
  event["id"] = id;
  emit(event);
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

/** Return the index of the entry with this name, or -1. */
static int findEntry(const char *name) {
  for (uint8_t i = 0; i < entryCount; i++) {
    if (strcmp(entries[i].name, name) == 0) {
      return i;
    }
  }
  return -1;
}

/** hello: start a new session. The bridge forgets entries, ids, the hold and the capture, without done. */
static void opHello(JsonObjectConst in) {
  entryCount = 0;
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

/** Fill e from a load op; return false for an absent or mistyped field, or one the bridge cannot send. */
static bool parseEntry(JsonObjectConst in, Entry &e) {
  const char *name = in["name"];
  const char *proto = in["proto"];
  const char *repeat = in["repeat"];
  JsonVariantConst raw = in["raw"];
  JsonVariantConst data = in["data"];
  if (name == nullptr || name[0] == '\0' || strlen(name) >= NAME_LEN || proto == nullptr || repeat == nullptr) {
    return false;
  }
  if (!in["addr"].is<uint32_t>() || !in["cmd"].is<uint32_t>() || !in["khz"].is<uint32_t>()) {
    return false;
  }
  if (!in["period_ms"].is<uint32_t>() || !raw.is<JsonArrayConst>() || raw.size() > MAX_RAW) {
    return false;
  }
  if (!data.isNull() && !data.is<uint32_t>()) {
    return false;
  }
  uint32_t addr = in["addr"];
  uint32_t cmd = in["cmd"];
  uint32_t khz = in["khz"];
  uint32_t periodMs = in["period_ms"];
  if (khz < 20 || khz > 60 || periodMs < 20 || periodMs > 1000) {
    return false;
  }
  e.nec = strcmp(proto, "NEC") == 0;
  if (!e.nec && (strcmp(proto, "raw") != 0 || raw.size() == 0)) {
    return false;
  }
  if (e.nec && (addr > 0xFFFF || cmd > 0xFF)) {
    return false;  // sendNEC carries a 16-bit address and an 8-bit command
  }
  if (strcmp(repeat, "nec") == 0) {
    e.repeat = REPEAT_NEC;
  } else if (strcmp(repeat, "resend") == 0) {
    e.repeat = REPEAT_RESEND;
  } else if (strcmp(repeat, "none") == 0) {
    e.repeat = REPEAT_NONE;
  } else {
    return false;
  }
  uint32_t frameUs = 0;
  e.rawLen = 0;
  for (JsonVariantConst v : raw.as<JsonArrayConst>()) {
    if (!v.is<uint16_t>() || v.as<uint16_t>() == 0) {
      return false;
    }
    frameUs += v.as<uint16_t>();
    e.raw[e.rawLen++] = v.as<uint16_t>();
  }
  if (frameUs > MAX_FRAME_US) {
    return false;
  }
  strcpy(e.name, name);
  e.hasData = !data.isNull();
  e.data = data | 0u;
  e.addr = addr;
  e.cmd = cmd;
  e.khz = khz;
  e.periodMs = periodMs;
  return true;
}

/** load: add one entry, or replace the entry with the same name. DISARMED only. */
static const char *opLoad(JsonObjectConst in, int64_t id) {
  if (state != DISARMED) {
    return "not disarmed";
  }
  Entry e;
  if (!parseEntry(in, e)) {
    return "bad entry";
  }
  int index = findEntry(e.name);
  if (index < 0) {
    if (entryCount >= MAX_ENTRIES) {
      return "table full";
    }
    index = entryCount++;
  }
  entries[index] = e;
  emitId("ack", id);
  return nullptr;
}

/** arm: n must equal the loaded count. DISARMED or FAULT go to READY; an armed bridge stays as it is. */
static const char *opArm(JsonObjectConst in, int64_t id) {
  JsonVariantConst n = in["n"];
  if (!n.is<int>() || n.as<int>() != entryCount) {
    return "entry count";
  }
  if (state == DISARMED || state == FAULT) {
    state = READY;
  }
  emitId("ack", id);
  return nullptr;
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
  const char *why;
  if (strcmp(op, "load") == 0) {
    why = opLoad(in, id);
  } else if (strcmp(op, "arm") == 0) {
    why = opArm(in, id);
  } else {
    why = "unknown op";
  }
  if (why != nullptr) {
    nack(idField, why);
  }
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
