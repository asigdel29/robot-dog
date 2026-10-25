"""Fake ESP32 IR bridge on a pty, with a simulated toy, for tests and the devstack.

It speaks the same NDJSON protocol as firmware/ir_bridge/ir_bridge.ino, so
robotd keeps one code path and only the device path changes. The simulated toy
starts at the floor center of 640 x 480 px and writes its pose to
sim_pose.json for the fake camera. Each line in and out goes to fake_esp32.log
with a monotonic timestamp, so tests can measure latency. SIGUSR1 simulates a
board reset.
"""

import argparse
import asyncio
import collections
import json
import math
import os
import signal
import time
import tty

FW = "fake-1"
LINE_LIMIT = 1024
HB_TIMEOUT_S = 1.0
FRAME_S = 0.07  # one full NEC frame on the air; the board runs nothing else meanwhile
REPEAT_FRAME_S = 0.012  # one NEC repeat frame
MIN_GAP_S = 0.04  # quiet time before the first frame of a new command (firmware MIN_GAP_MS)
MAX_ENTRIES = 24  # the firmware's RAM table
MAX_RAW = 120
MAX_FRAME_US = 150_000
NAME_LEN = 32  # firmware name buffer in bytes, with its terminating zero
REPEATS = ("nec", "resend", "none")
TICK_S = 0.01


def _uint(value, high):
    """Return True for an int (not a bool) in 0..high, as ArduinoJson's is<uintN_t>() decides."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= high


def entry_ok(msg):
    """Return True when the firmware can store and send this load entry; it refuses the rest as "bad entry"."""
    name, raw, data = msg.get("name"), msg.get("raw"), msg.get("data")
    nec = msg.get("proto") == "NEC"
    if not (isinstance(name, str) and 0 < len(name.encode()) < NAME_LEN and msg.get("repeat") in REPEATS):
        return False
    if not (_uint(msg.get("addr"), 0xFFFF if nec else 0xFFFFFFFF) and _uint(msg.get("cmd"), 0xFF if nec else 0xFFFFFFFF)):
        return False
    if not (_uint(msg.get("khz"), 60) and msg["khz"] >= 20 and _uint(msg.get("period_ms"), 1000)
            and msg["period_ms"] >= 20 and (data is None or _uint(data, 0xFFFFFFFF))):
        return False
    if not (isinstance(raw, list) and len(raw) <= MAX_RAW and all(_uint(v, 0xFFFF) and v > 0 for v in raw)):
        return False
    return sum(raw) <= MAX_FRAME_US and (nec or (msg.get("proto") == "raw" and len(raw) > 0))


def frame_s(entry, full):
    """Return how long one frame of an entry keeps the board busy."""
    if not full and entry["repeat"] == "nec":
        return REPEAT_FRAME_S
    return FRAME_S if entry["proto"] == "NEC" else sum(entry["raw"]) / 1e6


class FakeBridge:
    """Firmware state machine plus a toy that moves while drive commands arrive."""

    def __init__(self, master_fd, run_dir):
        self.fd = master_fd
        self.run_dir = run_dir
        self.boot = 0
        self.log = open(os.path.join(run_dir, "fake_esp32.log"), "a", buffering=1)
        self._buf = b""
        self.reset()

    def reset(self):
        """Return to the power-on state, which forgets the session and the registry, and send hello with sid 0."""
        self.boot += 1
        self.state = "DISARMED"
        self.sid = None
        self.last_id = 0
        self.entries = {}
        self.send = None  # (name, id) of a send that waits for the gap; later ops wait behind it
        self.air = None  # (end, events) of the frame on the air; the board does nothing else until end
        self.frame_end = -math.inf  # end of the last frame, for the gap
        self.queue = collections.deque()  # ops received and not yet applied
        self.last_hb = time.monotonic()
        self.max_loop_us = 0
        self.emit(ev="hello", sid=0, boot=self.boot, fw=FW, state=self.state)

    # ---- output

    def emit(self, **event):
        """Write one event line to the pty and the log."""
        line = json.dumps(event, separators=(",", ":"))
        self.log.write(f"{time.monotonic_ns()} out {line}\n")
        try:
            os.write(self.fd, (line + "\n").encode())
        except OSError:
            pass  # nobody reads the pty; a real UART also drops bytes

    # ---- input

    def readable(self):
        """Queue each JSON object line from robotd, then run the board loop."""
        try:
            data = os.read(self.fd, 4096)
        except OSError:
            return
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
        if len(self._buf) > LINE_LIMIT:
            self._buf = b""
        for raw in lines:
            if len(raw) > LINE_LIMIT:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self.log.write(f"{time.monotonic_ns()} in {raw.decode(errors='replace')}\n")  # arrival time
            if isinstance(msg, dict):
                self.queue.append(msg)
        self.step(time.monotonic())

    def on_op(self, msg, now):
        """Apply one op with the same rules as the firmware."""
        op = msg.get("op")
        if op == "hello":
            self.state = "DISARMED"
            self.sid = msg.get("sid")
            self.last_id = 0
            self.entries = {}
            self.emit(ev="hello", sid=self.sid, boot=self.boot, fw=FW, state=self.state)
            return
        if op == "hb":
            self.last_hb = now
            self.emit(ev="state", s=self.state, max_loop_us=self.max_loop_us, seq=msg.get("seq"))
            return
        op_id = msg.get("id")
        if self.sid is None:
            return self.emit(ev="nack", id=op_id, why="no hello")
        if not isinstance(op_id, int) or isinstance(op_id, bool) or op_id <= self.last_id:
            return self.emit(ev="nack", id=op_id, why="old id")
        self.last_id = op_id
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            return self.emit(ev="nack", id=op_id, why="unknown op")
        why = handler(msg, now)
        if why:
            self.emit(ev="nack", id=op_id, why=why)

    def op_load(self, msg, now):
        """load: add or replace one entry; DISARMED only."""
        if self.state != "DISARMED":
            return "not disarmed"
        if not entry_ok(msg):
            return "bad entry"
        if msg["name"] not in self.entries and len(self.entries) >= MAX_ENTRIES:
            return "table full"
        self.entries[msg["name"]] = msg
        self.emit(ev="ack", id=msg["id"])

    def op_arm(self, msg, now):
        """arm: n must equal the entry count; DISARMED or FAULT go to READY."""
        n = msg.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n != len(self.entries):
            return "entry count"
        if self.state in ("DISARMED", "FAULT"):
            self.state = "READY"
        self.last_hb = now
        self.emit(ev="ack", id=msg["id"])

    # ---- time

    def step(self, now):
        """One pass of the board loop: end the frame on the air, watchdog, ops, the send, hold frames."""
        if self.air is not None:
            if now < self.air[0]:
                return
            end, events = self.air
            self.air = None
            self.frame_end = end
            for event in events:
                self.emit(**event)
        self._watchdog(now)
        while self.queue and self.send is None and self.air is None:
            self.on_op(self.queue.popleft(), now)

    def _watchdog(self, now):
        """robotd silent while armed: stop output, send the stop entry once after the gap, then FAULT."""
        if self.state not in ("READY", "EXECUTING") or now - self.last_hb <= HB_TIMEOUT_S:
            return
        self.state = "FAULT"
        fault = {"ev": "fault", "why": "heartbeat"}
        if "stop" in self.entries:
            start = max(now, self.frame_end + MIN_GAP_S)
            self.air = (start + frame_s(self.entries["stop"], True), [{"ev": "tx", "id": 0, "frames": 1}, fault])
        else:
            self.emit(**fault)

    def tick(self, now, dt):
        """The board loop, then toy motion."""
        start = time.thread_time()  # CPU time: a busy CI host must not count as a slow loop
        self.step(now)
        self.max_loop_us = max(self.max_loop_us, int((time.thread_time() - start) * 1e6))


async def main():
    """Create the pty, print its path, and run the fake until killed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", default=os.environ.get("DOG_RUN_DIR", "/tmp/dog"))
    args = parser.parse_args()
    os.makedirs(args.run_dir, exist_ok=True)
    master, slave = os.openpty()
    tty.setraw(slave)  # keep one slave fd open so the master never sees EOF between clients
    os.set_blocking(master, False)
    slave_name = os.ttyname(slave)
    link = os.path.join(args.run_dir, "esp32")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(slave_name, link)
    bridge = FakeBridge(master, args.run_dir)
    loop = asyncio.get_running_loop()
    loop.add_reader(master, bridge.readable)
    loop.add_signal_handler(signal.SIGUSR1, bridge.reset)
    print(f"PTY {slave_name}", flush=True)
    last = time.monotonic()
    while True:
        await asyncio.sleep(TICK_S)
        now = time.monotonic()
        bridge.tick(now, now - last)
        last = now


if __name__ == "__main__":
    asyncio.run(main())
