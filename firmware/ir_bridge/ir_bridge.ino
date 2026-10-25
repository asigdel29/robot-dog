// ir_bridge.ino: firmware for the IR bridge between robotd on the Pi and the toy dog.
//
// Board: Adafruit ESP32 Feather V2. IR module: Adafruit IR transceiver, IRin on
// GPIO33 (receiver output), IRout on GPIO27 (emitter driver input). robotd talks to
// the board over USB serial at 115200 baud, one JSON object per line. The wire
// protocol is docs/design/protocol.md section 2; sim/fake_esp32.py is the reference
// behavior and tests/test_conformance.py checks both. The bridge owns the hard
// time limits (lease deadman, heartbeat watchdog), so a stalled Pi cannot keep the
// dog moving. robotd sends the IR registry to RAM at each handshake, so a new
// capture never needs a new flash.

// IRremote reads these before its include. Frames with more durations than the
// registry can store (MAX_RAW) must still be seen as too long, so the buffer has spare room.
#define RAW_BUFFER_LENGTH 124
// Calibration knobs for the receiver module, kept at the library defaults until a
// capture on the real remote says otherwise: MARK_EXCESS_MICROS (20) corrects the
// receiver's mark stretch; RECORD_GAP_MICROS (8000) is the space that ends a frame.

#include <WiFi.h>
#include <IRremote.hpp>
#include <ArduinoJson.h>

static const char FW_VERSION[] = "ir_bridge-1";  ///< reported in hello, so robotd logs which build runs
static const uint8_t IR_IN_PIN = 33;   ///< IRin pad: receiver output into the ESP32
static const uint8_t IR_OUT_PIN = 27;  ///< IRout pad: ESP32 output into the emitter driver
static const size_t LINE_LIMIT = 1024;  ///< longest line in either direction; longer input lines are discarded
static const uint32_t HB_TIMEOUT_MS = 1000;  ///< robotd silence that trips the watchdog while armed
static const uint16_t MAX_LEASE_MS = 400;  ///< longest motion that one hold op can buy
static const uint8_t MAX_ENTRIES = 24;  ///< RAM table size; the remote has 12 buttons
static const uint8_t MAX_RAW = 120;  ///< raw durations per entry, the same cap as robotd's registry rule
static const size_t NAME_LEN = 32;   ///< entry name buffer, with the terminating zero

/** Bridge states from protocol.md; STOPPING lasts while the watchdog sends the stop entry. */
enum State : uint8_t { DISARMED, READY, EXECUTING, STOPPING, FAULT };
static const char *const STATE_NAMES[] = {"DISARMED", "READY", "EXECUTING", "STOPPING", "FAULT"};

/** How the remote repeats a held button: NEC repeat frame, the full frame again, or nothing. */
enum Repeat : uint8_t { REPEAT_NEC, REPEAT_RESEND, REPEAT_NONE };

/** One registry command in RAM: enough to send its first frame and its repeat frames. */
struct Entry {
  char name[NAME_LEN];     ///< registry name that send and hold refer to
  bool nec;                ///< true: encode with sendNEC; false: replay raw
  uint16_t addr;           ///< NEC address, 8 or 16 bit
  uint8_t cmd;             ///< NEC command
  uint8_t khz;             ///< carrier for raw replay
  Repeat repeat;           ///< what a held button sends after the first frame
  uint16_t periodMs;       ///< frame start to frame start while held
  uint8_t rawLen;          ///< used part of raw
  uint16_t raw[MAX_RAW];   ///< microseconds, mark first
};

/** The one held command: its lease is the firmware deadman. */
struct Hold {
  bool active;          ///< a hold owns the emitter
  uint8_t entry;        ///< index into entries
  int64_t id;           ///< the latest hold op, which tx and done report
  uint32_t leaseEnd;    ///< millis() at which the deadman ends the hold
  uint32_t nextFrame;   ///< millis() at which the next frame is due
  bool first;           ///< the first frame is always a full frame
};

/** The one capture window; the receiver runs only inside it, so it never decodes our own frames. */
struct Capture {
  bool active;              ///< the receiver runs
  int64_t id;               ///< the capture op, which done reports
  uint32_t end;             ///< millis() at which the window closes
  bool first;               ///< no frame yet, so gap_us is 0
  uint32_t prevFrameTicks;  ///< length of the last frame, for the start-to-start gap
};

static State state = DISARMED;     ///< start-up never moves the dog
static bool haveSession = false;   ///< false until the first hello; ops before it are refused
static int64_t lastId = 0;         ///< ids must grow within a session, so a replayed op is refused
static uint32_t bootId = 0;        ///< random at boot, so robotd can tell a reset
static Entry entries[MAX_ENTRIES]; ///< the registry, loaded by robotd at each hello
static uint8_t entryCount = 0;     ///< loaded part of entries; arm checks it
static Hold hold = {};             ///< the one held command
static Capture capture = {};       ///< the one capture window
static uint32_t lastHbMs = 0;      ///< millis() of the last hb or arm, for the watchdog
static uint32_t maxLoopUs = 0;     ///< worst loop pass since boot, reported in state
static char lineBuf[LINE_LIMIT + 1];  ///< the serial line being received
static size_t lineLen = 0;         ///< used part of lineBuf
static bool lineTooLong = false;   ///< discard input until the next newline

/** True when millis() time t has come; correct across the 49-day wrap. */
static bool reached(uint32_t now, uint32_t t) {
  return (int32_t)(now - t) >= 0;
}

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

/** Report one IR frame sent for an op; id 0 is the watchdog stop. */
static void emitTx(int64_t id) {
  JsonDocument event;
  event["ev"] = "tx";
  event["id"] = id;
  event["frames"] = 1;
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

/** Send one frame of an entry. This blocks for the frame time, the longest block in the loop. */
static void transmit(const Entry &e, bool full) {
  if (!full && e.repeat == REPEAT_NEC) {
    IrSender.sendNECRepeat();
  } else if (e.nec) {
    IrSender.sendNEC(e.addr, e.cmd, 0);
  } else {
    IrSender.sendRaw(e.raw, e.rawLen, e.khz);
  }
}

/** End the hold with its done event, and leave EXECUTING. */
static void endHold() {
  if (hold.active) {
    hold.active = false;
    emitId("done", hold.id);
  }
  if (state == EXECUTING) {
    state = READY;
  }
}

/** Stop the receiver; a capture that a hello ends gets no done, as robotd has forgotten its id. */
static void stopCapture() {
  if (capture.active) {
    IrReceiver.stop();
    capture.active = false;
  }
}

/** hello: start a new session. The bridge forgets entries, ids, the hold and the capture. */
static void opHello(JsonObjectConst in) {
  hold.active = false;
  stopCapture();
  entryCount = 0;
  lastId = 0;
  state = DISARMED;
  haveSession = true;
  emitHello(in["sid"] | 0LL);
}

/** hb: feed the watchdog and report the state. */
static void opHb(JsonObjectConst in) {
  lastHbMs = millis();
  JsonDocument event;
  event["ev"] = "state";
  event["s"] = STATE_NAMES[state];
  event["max_loop_us"] = maxLoopUs;
  event["seq"] = in["seq"];
  emit(event);
}

/** Fill e from a load op; return false for a field that the firmware cannot send as asked. */
static bool parseEntry(JsonObjectConst in, Entry &e) {
  const char *name = in["name"];
  const char *proto = in["proto"];
  const char *repeat = in["repeat"];
  JsonArrayConst raw = in["raw"];
  uint32_t addr = in["addr"] | 0u;
  uint32_t cmd = in["cmd"] | 0u;
  uint32_t khz = in["khz"] | 38u;
  uint32_t periodMs = in["period_ms"] | 108u;
  if (name == nullptr || name[0] == '\0' || strlen(name) >= NAME_LEN || proto == nullptr || repeat == nullptr) {
    return false;
  }
  if (khz < 20 || khz > 60 || periodMs < 10 || periodMs > 1000 || raw.size() > MAX_RAW) {
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
  e.rawLen = 0;
  for (JsonVariantConst v : raw) {
    uint32_t us = v | 0u;
    if (!v.is<uint32_t>() || us == 0 || us > 0xFFFF) {
      return false;
    }
    e.raw[e.rawLen++] = us;
  }
  strcpy(e.name, name);
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
  if (capture.active) {
    return "capturing";
  }
  if (state == DISARMED || state == FAULT) {
    state = READY;
  }
  lastHbMs = millis();
  emitId("ack", id);
  return nullptr;
}

/** send: one full frame, READY only. */
static const char *opSend(JsonObjectConst in, int64_t id) {
  if (state != READY) {
    return "not ready";  // EXECUTING means a hold owns the emitter
  }
  int index = findEntry(in["name"] | "");
  if (index < 0) {
    return "unknown name";
  }
  emitId("ack", id);
  transmit(entries[index], true);
  emitTx(id);
  emitId("done", id);
  return nullptr;
}

/** hold: repeat a command until the lease ends; a hold of the same name only extends the lease. */
static const char *opHold(JsonObjectConst in, int64_t id) {
  if (state != READY && state != EXECUTING) {
    return "not ready";
  }
  int index = findEntry(in["name"] | "");
  if (index < 0) {
    return "unknown name";
  }
  JsonVariantConst lease = in["lease_ms"];
  if (!lease.is<int>() || lease.as<int>() < 1 || lease.as<int>() > MAX_LEASE_MS) {
    return "lease";
  }
  uint32_t now = millis();
  if (!hold.active || hold.entry != index) {
    hold.entry = index;
    hold.nextFrame = now;
    hold.first = true;
  }
  hold.active = true;
  hold.id = id;
  hold.leaseEnd = now + lease.as<int>();
  state = EXECUTING;
  emitId("ack", id);
  return nullptr;
}

/** stop: end the hold now; the bridge stays armed. */
static const char *opStop(int64_t id) {
  endHold();
  emitId("ack", id);
  return nullptr;
}

/** disarm: end the hold and go to DISARMED. */
static const char *opDisarm(int64_t id) {
  endHold();
  state = DISARMED;
  emitId("ack", id);
  return nullptr;
}

/** capture: run the receiver for ms and report each frame. DISARMED only, so nothing transmits meanwhile. */
static const char *opCapture(JsonObjectConst in, int64_t id) {
  if (state != DISARMED) {
    return "capture needs DISARMED";
  }
  if (capture.active) {
    return "capturing";
  }
  emitId("ack", id);
  capture.active = true;
  capture.id = id;
  capture.end = millis() + (in["ms"] | 0u);
  capture.first = true;
  IrReceiver.begin(IR_IN_PIN, ENABLE_LED_FEEDBACK);
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
  } else if (strcmp(op, "send") == 0) {
    why = opSend(in, id);
  } else if (strcmp(op, "hold") == 0) {
    why = opHold(in, id);
  } else if (strcmp(op, "stop") == 0) {
    why = opStop(id);
  } else if (strcmp(op, "disarm") == 0) {
    why = opDisarm(id);
  } else if (strcmp(op, "capture") == 0) {
    why = opCapture(in, id);
  } else {
    why = "unknown op";
  }
  if (why != nullptr) {
    nack(idField, why);
  }
}

/** Read serial input and handle at most one line, so one pass never runs more than one op. */
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

/** Heartbeat watchdog: robotd silent for HB_TIMEOUT_MS while armed means stop, send `stop` once, FAULT. */
static void checkHeartbeat() {
  if ((state != READY && state != EXECUTING) || millis() - lastHbMs <= HB_TIMEOUT_MS) {
    return;
  }
  hold.active = false;
  state = STOPPING;
  int stop = findEntry("stop");
  if (stop >= 0) {
    transmit(entries[stop], true);
    emitTx(0);
  }
  state = FAULT;
  JsonDocument event;
  event["ev"] = "fault";
  event["why"] = "heartbeat";
  emit(event);
}

/** Lease expiry and repeat frames of the hold, scheduled from frame start so the period does not drift. */
static void serviceHold() {
  if (!hold.active) {
    return;
  }
  uint32_t now = millis();
  if ((int32_t)(now - hold.leaseEnd) > 0) {
    endHold();
    return;
  }
  const Entry &e = entries[hold.entry];
  if (!reached(now, hold.nextFrame) || (!hold.first && e.repeat == REPEAT_NONE)) {
    return;
  }
  transmit(e, hold.first);
  hold.first = false;
  hold.nextFrame = now + e.periodMs;
  emitTx(hold.id);
}

/** Report one received frame as a cap event; raw is in microseconds with the receiver's mark stretch removed. */
static void emitCap() {
  const IRData &d = IrReceiver.decodedIRData;
  JsonDocument event;
  event["ev"] = "cap";
  event["proto"] = d.protocol == UNKNOWN ? "raw" : getProtocolString(d.protocol);
  event["addr"] = d.address;
  event["cmd"] = d.command;
  event["bits"] = d.numberOfBits;
  JsonArray raw = event["raw"].to<JsonArray>();
  bool fits = d.rawlen <= MAX_RAW + 1;  // rawbuf[0] is unused; a longer frame was cut by the buffer
  uint32_t frameTicks = 0;
  for (IRRawlenType i = 1; i < d.rawlen; i++) {
    int32_t ticks = IrReceiver.irparams.rawbuf[i];
    frameTicks += ticks;
    if (fits) {
      int32_t us = ticks * MICROS_PER_TICK;
      raw.add(i & 1 ? us - MARK_EXCESS_MICROS : us + MARK_EXCESS_MICROS);  // odd index: mark
    }
  }
  // Start-to-start time, the value robotctl uses as period_ms; ISR ticks, so loop jitter does not count.
  event["gap_us"] = capture.first ? 0 : (capture.prevFrameTicks + d.initialGapTicks) * MICROS_PER_TICK;
  // A repeat is a short frame without data bits (the NEC repeat frame); a full frame sent again is not.
  event["repeat"] = (d.flags & IRDATA_FLAGS_IS_REPEAT) != 0 && d.numberOfBits == 0;
  capture.first = false;
  capture.prevFrameTicks = frameTicks;
  emit(event);
}

/** Report frames while the capture window is open, then stop the receiver and send done. */
static void serviceCapture() {
  if (!capture.active) {
    return;
  }
  if (IrReceiver.decode()) {
    emitCap();
    IrReceiver.resume();
  }
  if (reached(millis(), capture.end)) {
    stopCapture();
    emitId("done", capture.id);
  }
}

/** Boot: radios off, emitter off, serial up, then one hello with sid 0. Start-up never transmits. */
void setup() {
  WiFi.mode(WIFI_OFF);
  btStop();
  pinMode(IR_OUT_PIN, OUTPUT);
  digitalWrite(IR_OUT_PIN, LOW);
  IrSender.begin(IR_OUT_PIN);
  Serial.setRxBufferSize(1024);  // one raw frame blocks the loop for about 70 ms
  Serial.setTxBufferSize(2048);  // a cap line must not block the loop
  Serial.begin(115200);
  bootId = esp_random() >> 1;  // differs at each reset, so robotd can see one
  emitHello(0);
}

/** One pass: watchdog, one op, hold frames, capture frames. max_loop_us is the worst pass. */
void loop() {
  uint32_t start = micros();
  checkHeartbeat();
  readSerial();
  serviceHold();
  serviceCapture();
  uint32_t took = micros() - start;
  if (took > maxLoopUs) {
    maxLoopUs = took;
  }
}
