"""Fake ESP32 IR bridge on a pty, with a simulated toy, for tests and the devstack.

It speaks the same NDJSON protocol as firmware/ir_bridge/ir_bridge.ino, so
robotd keeps one code path and only the device path changes. The simulated toy
drives on an image-space floor of 640 x 480 px and writes its pose to
sim_pose.json for the fake camera. Each line in and out goes to fake_esp32.log
with a monotonic timestamp, so tests can measure latency. SIGUSR1 simulates a
board reset.
"""

import argparse
import asyncio
import json
import math
import os
import signal
import time
import tty

FW = "fake-1"
LINE_LIMIT = 1024
HB_TIMEOUT_S = 1.0
FRAME_S = 0.07
MAX_LEASE_MS = 400
TICK_S = 0.01
POSE_EVERY_S = 0.1
FLOOR = (640.0, 480.0)
#: Toy motion per drive command name: (px/s forward, rad/s clockwise on screen).
MOTION = {"forward": (120.0, 0.0), "back": (-80.0, 0.0),
          "left": (0.0, -math.radians(90)), "right": (0.0, math.radians(90))}
NEC_FRAME_US = [9000, 4500] + [560, 560] * 16 + [560, 1690] * 16 + [560]


class FakeBridge:
    """Firmware state machine plus a toy that moves while drive commands arrive."""

    def __init__(self, master_fd, run_dir, start_pose):
        self.fd = master_fd
        self.run_dir = run_dir
        self.boot = 0
        self.log = open(os.path.join(run_dir, "fake_esp32.log"), "a", buffering=1)
        self.pose = list(start_pose)
        self.reset()
        self._buf = b""
        self._next_pose_write = 0.0

    def reset(self):
        """Return to the power-on state; the board forgets its session and registry."""
        self.boot += 1
        self.state = "DISARMED"
        self.sid = None
        self.last_id = 0
        self.entries = {}
        self.hold = None  # dict(name, id, lease_end, next_frame)
        self.timed = None  # (name, until) for a send that moves the toy
        self.last_hb = time.monotonic()
        self.max_loop_us = 0

    # ---- output

    def emit(self, **event):
        line = json.dumps(event, separators=(",", ":"))
        self.log.write(f"{time.monotonic_ns()} out {line}\n")
        try:
            os.write(self.fd, (line + "\n").encode())
        except (BlockingIOError, OSError):
            pass  # nobody reads the pty; a real UART also drops bytes

    # ---- input

    def readable(self):
        try:
            data = os.read(self.fd, 4096)
        except (BlockingIOError, OSError):
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
            self.log.write(f"{time.monotonic_ns()} in {raw.decode(errors='replace')}\n")
            if isinstance(msg, dict):
                self.on_op(msg)

    def on_op(self, msg):
        """Apply one op with the same rules as the firmware."""
        op = msg.get("op")
        now = time.monotonic()
        if op == "hello":
            self.state = "DISARMED"
            self.sid = msg.get("sid")
            self.last_id = 0
            self.entries = {}
            self.hold = None
            self.emit(ev="hello", sid=self.sid, boot=self.boot, fw=FW, state=self.state)
            return
        if op == "hb":
            self.last_hb = now
            self.emit(ev="state", s=self.state, max_loop_us=self.max_loop_us, seq=msg.get("seq"))
            return
        op_id = msg.get("id")
        if self.sid is None:
            return self.emit(ev="nack", id=op_id, why="no hello")
        if not isinstance(op_id, int) or op_id <= self.last_id:
            return self.emit(ev="nack", id=op_id, why="old id")
        self.last_id = op_id
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            return self.emit(ev="nack", id=op_id, why="unknown op")
        why = handler(msg, now)
        if why:
            self.emit(ev="nack", id=op_id, why=why)

    def op_load(self, msg, now):
        if self.state != "DISARMED":
            return "not disarmed"
        self.entries[msg["name"]] = msg
        self.emit(ev="ack", id=msg["id"])

    def op_arm(self, msg, now):
        if msg.get("n") != len(self.entries):
            return "entry count"
        self.state = "READY"
        self.last_hb = now
        self.emit(ev="ack", id=msg["id"])

    def op_send(self, msg, now):
        if self.state not in ("READY", "EXECUTING") or self.hold is not None:
            return "not ready"
        if msg.get("name") not in self.entries:
            return "unknown name"
        self.emit(ev="ack", id=msg["id"])
        name = msg["name"]
        self.timed = (name, now + 0.3) if name in MOTION else None
        asyncio.get_running_loop().call_later(FRAME_S, self._sent, msg["id"])

    def _sent(self, op_id):
        self.emit(ev="tx", id=op_id, frames=1)
        self.emit(ev="done", id=op_id)

    def op_hold(self, msg, now):
        if self.state not in ("READY", "EXECUTING"):
            return "not ready"
        if msg.get("name") not in self.entries:
            return "unknown name"
        lease = msg.get("lease_ms")
        if not isinstance(lease, int) or not 0 < lease <= MAX_LEASE_MS:
            return "lease"
        if self.hold is not None and self.hold["name"] == msg["name"]:
            self.hold.update(id=msg["id"], lease_end=now + lease / 1000)
        else:
            self.hold = {"name": msg["name"], "id": msg["id"], "lease_end": now + lease / 1000, "next_frame": now}
        self.state = "EXECUTING"
        self.emit(ev="ack", id=msg["id"])

    def op_stop(self, msg, now):
        self._end_hold()
        self.timed = None
        self.emit(ev="ack", id=msg["id"])

    def op_disarm(self, msg, now):
        self._end_hold()
        self.timed = None
        self.state = "DISARMED"
        self.emit(ev="ack", id=msg["id"])

    def op_capture(self, msg, now):
        if self.state != "DISARMED":
            return "capture needs DISARMED"
        ms = msg.get("ms", 0)
        self.emit(ev="ack", id=msg["id"])
        loop = asyncio.get_running_loop()
        frame = {"proto": "NEC", "addr": 0, "cmd": 0x18, "bits": 32, "raw": NEC_FRAME_US, "gap_us": 0}
        loop.call_later(min(0.2, ms / 2000), lambda: self.emit(ev="cap", **frame))
        if ms >= 4000:  # a long capture window stands for a held button: NEC repeat frames
            for i in range(1, 6):
                loop.call_later(min(0.2, ms / 2000) + i * 0.108, lambda: self.emit(
                    ev="cap", proto="NEC", addr=0, cmd=0x18, bits=0, raw=[9000, 2250, 560], gap_us=108000, repeat=True))
        loop.call_later(ms / 1000, lambda: self.emit(ev="done", id=msg["id"]))

    def _end_hold(self):
        if self.hold is not None:
            self.emit(ev="done", id=self.hold["id"])
            self.hold = None
        if self.state == "EXECUTING":
            self.state = "READY"

    # ---- time

    def tick(self, now, dt):
        """Lease expiry, repeat frames, heartbeat watchdog and toy motion."""
        start = time.thread_time()  # CPU time: a busy CI host must not count as a slow loop
        if self.state in ("READY", "EXECUTING") and now - self.last_hb > HB_TIMEOUT_S:
            self.hold = None
            self.timed = None
            if "stop" in self.entries:
                self.emit(ev="tx", id=0, frames=1)
            self.state = "FAULT"
            self.emit(ev="fault", why="heartbeat")
        if self.hold is not None:
            if now > self.hold["lease_end"]:
                self._end_hold()
            elif now >= self.hold["next_frame"]:
                self.emit(ev="tx", id=self.hold["id"], frames=1)
                period = self.entries[self.hold["name"]].get("period_ms", 108) / 1000
                self.hold["next_frame"] = now + period
        moving = self.hold["name"] if self.hold else (self.timed[0] if self.timed and now < self.timed[1] else None)
        speed, turn = MOTION.get(moving, (0.0, 0.0))
        x, y, heading = self.pose
        heading += turn * dt
        x = min(FLOOR[0], max(0.0, x + math.cos(heading) * speed * dt))
        y = min(FLOOR[1], max(0.0, y + math.sin(heading) * speed * dt))
        self.pose = [x, y, math.atan2(math.sin(heading), math.cos(heading))]
        if now >= self._next_pose_write:
            self._next_pose_write = now + POSE_EVERY_S
            self._write_pose()
        self.max_loop_us = max(self.max_loop_us, int((time.thread_time() - start) * 1e6))

    def _write_pose(self):
        path = os.path.join(self.run_dir, "sim_pose.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as handle:
            json.dump({"x": self.pose[0], "y": self.pose[1], "heading": self.pose[2],
                       "t_mono_ns": time.monotonic_ns()}, handle)
        os.replace(tmp, path)


async def main():
    """Create the pty, print its path, and run the fake until killed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", default=os.environ.get("DOG_RUN_DIR", "/tmp/dog"))
    parser.add_argument("--pose", default="320,240,0", help="start x,y,heading_rad")
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
    bridge = FakeBridge(master, args.run_dir, [float(v) for v in args.pose.split(",")])
    loop = asyncio.get_running_loop()
    loop.add_reader(master, bridge.readable)
    loop.add_signal_handler(signal.SIGUSR1, lambda: (bridge.reset(), bridge.emit(
        ev="hello", sid=0, boot=bridge.boot, fw=FW, state=bridge.state)))
    print(f"PTY {slave_name}", flush=True)
    bridge.emit(ev="hello", sid=0, boot=bridge.boot, fw=FW, state=bridge.state)
    last = time.monotonic()
    while True:
        await asyncio.sleep(TICK_S)
        now = time.monotonic()
        bridge.tick(now, now - last)
        last = now


if __name__ == "__main__":
    asyncio.run(main())
