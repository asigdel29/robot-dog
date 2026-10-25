"""robotd: the only process that talks to the ESP32 IR bridge.

It owns the 20 Hz loop, the trick registry, the arm state, the one running
action and the safety rules. It serves robotd.sock (local clients) and
robotd-cloud.sock (linkd only) and never opens an outbound connection, so the
loop never waits on another service. The design follows microduck's robotd;
docs/design/safety.md and docs/design/registry.md own the rules.
"""

import argparse
import asyncio
import collections
import contextlib
import itertools
import json
import logging
import math
import os
import random
import signal
import socket
import sys
import termios
import time
import tomllib
import tty

import dogproto
from dogproto import RpcError

log = logging.getLogger("robotd")

LOOP_HZ = 20
HEARTBEAT_TICKS = 4
DEADMAN_S = 0.5
ARM_IDLE_S = 600
ESP_SILENCE_S = 1.0
HELLO_WAIT_S = 5.0
ACK_WAIT_S = 0.5
STICK_DEADBAND = 0.5
HOLD_LEASE_MS = 300  # firmware caps leases at 400; 300 gives three renewals of margin
HOLD_RENEW_S = 0.1
VISION_MAX_AGE_S = 2.0
GOTO_LIMIT_S = 30
GOTO_MAX_STEPS = 20
GOTO_NO_PROGRESS_STEPS = 5
TURN_THRESHOLD_RAD = math.radians(25)
RESULT_CACHE = 256
MAX_TICK_ERRORS = 10
DEGRADED_HZ = 15
DEGRADED_WINDOWS = 5
ESP_LINE_LIMIT = 1024

PROTOS = ("NEC", "raw")
KINDS = ("hold", "episodic", "perpetual", "mode")
REPEATS = ("nec", "resend", "none")
DIRS = ("forward", "left", "right", "back")
MAX_RAW = 120
MAX_FRAME_US = 150_000  # bridge limits, protocol.md 2.4: a registry inside them is never refused at load
MAX_ENTRIES = 24
MAX_NAME_BYTES = 31
TOP_FIELDS = {"schema", "robot", "commands", "drive", "goto"}
IR_FIELDS = {"proto", "addr", "cmd", "bits", "raw_us", "carrier_khz", "data"}
GOTO_DEFAULTS = {"robot_marker": 1, "goal_radius_px": 40, "area_px": [[0, 0], [640, 0], [640, 480], [0, 480]],
                 "turn_ms": 200, "forward_ms": 300, "settle_ms": 300, "turn_sign": 1}
CMD_DEFAULTS = {"ir": None, "kind": "episodic", "repeat": "none", "period_ms": 108, "stop_via": "none",
                "natural_ms": None, "max_run_ms": 500, "interruptible": None, "verified": False,
                "cloud": False, "say": "", "notes": ""}


# ---------------------------------------------------------------- registry


def _int_in(value, low, high):
    """Return True for an int (not a bool) in low..high."""
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _num(value):
    """Return True for a finite int or float (not a bool)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def ir_errors(ir):
    """Return why an IR payload is refused: an unknown field, or outside the bridge limits (protocol.md 2.4)."""
    if not isinstance(ir, dict):
        return ["ir must be an object"]
    proto, raw = ir.get("proto"), ir.get("raw_us", [])
    errors = [f"unknown ir field {k}" for k in ir if k not in IR_FIELDS]
    if proto not in PROTOS:
        errors.append(f"protocol {proto} refused; only NEC and raw (no toggle bit)")
    if proto == "NEC" and not (_int_in(ir.get("addr"), 0, 0xFFFF) and _int_in(ir.get("cmd"), 0, 0xFF)):
        errors.append("NEC needs addr 0..0xFFFF and cmd 0..0xFF")
    if proto == "raw" and not (_int_in(ir.get("addr", 0), 0, 0xFFFFFFFF) and _int_in(ir.get("cmd", 0), 0, 0xFFFFFFFF)):
        errors.append("addr and cmd must be unsigned ints")
    if "data" in ir and not _int_in(ir["data"], 0, 0xFFFFFFFF):
        errors.append("data must be an int in 0..0xFFFFFFFF")
    if not (isinstance(raw, list) and len(raw) <= MAX_RAW and all(_int_in(v, 1, 0xFFFF) for v in raw)):
        errors.append(f"raw_us must be a list of {MAX_RAW} or fewer ints in 1..65535")
    elif sum(raw) > MAX_FRAME_US:
        errors.append(f"raw_us frame is longer than {MAX_FRAME_US // 1000} ms")
    elif proto == "raw" and not raw:
        errors.append("a raw payload needs raw_us")
    if not _int_in(ir.get("carrier_khz", 38), 20, 60):
        errors.append("carrier_khz must be an int in 20..60")
    return errors


def normalize(doc):
    """Return a copy of a registry with defaults filled in for absent fields."""
    out = dict(doc)
    out["commands"] = {name: {**CMD_DEFAULTS, **cmd} for name, cmd in doc.get("commands", {}).items()}
    out["drive"] = {**{d: None for d in DIRS}, **doc.get("drive", {})}
    out["goto"] = {**GOTO_DEFAULTS, **doc.get("goto", {})}
    return out


def check_registry(doc):
    """Return the rule violations of a registry (registry.md section 3); an empty list means robotd may arm."""
    if not isinstance(doc, dict) or not all(isinstance(doc.get(k, {}), dict) for k in ("commands", "drive", "goto")):
        return ["the registry, commands, drive and goto must be objects"]
    if not all(isinstance(cmd, dict) for cmd in doc.get("commands", {}).values()):
        return ["each command must be an object"]
    errors = [f"unknown field {k}" for k in doc if k not in TOP_FIELDS]
    if doc.get("schema") != 1:
        errors.append("schema must be 1")
    errors += [f"drive: unknown field {k}" for k in doc.get("drive", {}) if k not in DIRS]
    errors += [f"goto: unknown field {k}" for k in doc.get("goto", {}) if k not in GOTO_DEFAULTS]
    norm = normalize(doc)
    cmds = norm["commands"]
    for name, cmd in cmds.items():
        errors += [f"{name}: unknown field {k}" for k in cmd if k not in CMD_DEFAULTS]
        if cmd["ir"] is not None:
            errors += [f"{name}: {e}" for e in ir_errors(cmd["ir"])]
        if not 0 < len(name.encode()) <= MAX_NAME_BYTES:
            errors.append(f"{name}: a command name must be 1 to {MAX_NAME_BYTES} bytes in UTF-8")
        if cmd["kind"] not in KINDS:
            errors.append(f"{name}: kind must be one of {KINDS}")
        if cmd["repeat"] not in REPEATS:
            errors.append(f"{name}: repeat must be one of {REPEATS}")
        if not all(isinstance(cmd[flag], bool) for flag in ("verified", "cloud")):
            errors.append(f"{name}: verified and cloud must be true or false")
        if not _int_in(cmd["period_ms"], 20, 1000):
            errors.append(f"{name}: period_ms must be an int in 20..1000")
        if not _int_in(cmd["max_run_ms"], 1, 10000):
            errors.append(f"{name}: max_run_ms must be an int in 1..10000")
        if cmd["natural_ms"] is not None and not _int_in(cmd["natural_ms"], 0, 60000):
            errors.append(f"{name}: natural_ms must be null or an int in 0..60000")
        if cmd["verified"] and cmd["ir"] is None:
            errors.append(f"{name}: verified without an IR payload")
        if cmd["kind"] == "hold" and not _int_in(cmd["max_run_ms"], 1, 1000):
            errors.append(f"{name}: hold needs max_run_ms in 1..1000")
        if not isinstance(cmd["stop_via"], str):
            errors.append(f"{name}: stop_via must be a string")
        elif cmd["kind"] == "perpetual" and cmd["verified"] and not cmds.get(cmd["stop_via"], {}).get("verified"):
            errors.append(f"{name}: perpetual needs stop_via naming a verified command")
        if cmd["cloud"]:
            if not cmd["verified"]:
                errors.append(f"{name}: cloud needs verified")
            if cmd["kind"] == "mode":
                errors.append(f"{name}: a mode command can never be cloud")
            if cmd["kind"] == "episodic" and not _int_in(cmd["natural_ms"], 0, 10000):
                errors.append(f"{name}: episodic cloud needs natural_ms <= 10000")
    if sum(cmd["ir"] is not None for cmd in cmds.values()) > MAX_ENTRIES:
        errors.append(f"more than {MAX_ENTRIES} commands with an IR payload; the bridge keeps {MAX_ENTRIES}")
    for direction, target in norm["drive"].items():
        if target is not None and not (isinstance(target, str) and cmds.get(target, {}).get("kind") == "hold"):
            errors.append(f"drive.{direction}: {target} is not a hold command")
    goto = norm["goto"]
    if not _int_in(goto["robot_marker"], 0, 999):
        errors.append("goto.robot_marker must be an int in 0..999")
    if not (_num(goto["goal_radius_px"]) and goto["goal_radius_px"] > 0):
        errors.append("goto.goal_radius_px must be a positive number")
    for key in ("turn_ms", "forward_ms", "settle_ms"):
        if not _int_in(goto[key], 0, 1000):
            errors.append(f"goto.{key} must be an int in 0..1000")
    if goto["turn_sign"] not in (1, -1):
        errors.append("goto.turn_sign must be 1 or -1")
    area = goto["area_px"]
    if not (isinstance(area, list) and len(area) >= 3
            and all(isinstance(p, list) and len(p) == 2 and all(_num(v) for v in p) for p in area)):
        errors.append("goto.area_px must be a list of three or more [x, y] points")
    return errors


def capabilities(doc, cloud=False):
    """Return the usable drive directions and goto of a valid registry; cloud=True also needs each cloud flag."""
    norm = normalize(doc)
    cmds = norm["commands"]
    drive = {d: bool(t in cmds and cmds[t]["verified"] and (cmds[t]["cloud"] or not cloud))
             for d, t in norm["drive"].items()}
    return {"drive": drive, "goto": drive["forward"] and (drive["left"] or drive["right"])}


def ir_wire(name, cmd):
    """Return the load op fields for one command."""
    ir = cmd["ir"]
    wire = {"name": name, "proto": ir["proto"], "addr": ir.get("addr", 0), "cmd": ir.get("cmd", 0),
            "raw": ir.get("raw_us", []), "khz": ir.get("carrier_khz", 38),
            "repeat": cmd["repeat"], "period_ms": cmd["period_ms"]}
    if "data" in ir:
        wire["data"] = ir["data"]  # the captured NEC frame, sent byte for byte
    return wire


# ---------------------------------------------------------------- pure helpers


def stick_dir(vx, vyaw):
    """Map stick axes to one drive direction, or None inside the dead band; the larger axis wins."""
    if max(abs(vx), abs(vyaw)) < STICK_DEADBAND:
        return None
    if abs(vx) >= abs(vyaw):
        return "forward" if vx > 0 else "back"
    return "left" if vyaw > 0 else "right"


def wrap(angle):
    """Wrap an angle to (-pi, pi]."""
    return math.atan2(math.sin(angle), math.cos(angle))


def nav_step(robot, goal, radius, turn_sign=1):
    """Return "done", "left", "right" or "forward" for one goto step from robot (cx, cy, heading) to goal (cx, cy).

    Image y points down, so a positive heading error is a right turn when the
    camera looks straight down; turn_sign = -1 swaps the turns for a mirrored view.
    """
    dx, dy = goal[0] - robot[0], goal[1] - robot[1]
    if math.hypot(dx, dy) <= radius:
        return "done"
    err = wrap(math.atan2(dy, dx) - robot[2]) * turn_sign
    if abs(err) > TURN_THRESHOLD_RAD:
        return "right" if err > 0 else "left"
    return "forward"


def inside(point, polygon):
    """Return True when point is inside polygon (ray cast)."""
    x, y = point
    hit = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def sd_notify(state):
    """Send one systemd notify message; do nothing outside systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock, contextlib.suppress(OSError):
        sock.sendto(state.encode(), addr)


# ---------------------------------------------------------------- ESP32 link


class EspLink:
    """NDJSON link to the IR bridge over a tty; robotd is its only writer."""

    def __init__(self, path, on_message, on_lost):
        self.path = path
        self._on_message = on_message
        self._on_lost = on_lost
        self._fd = None
        self._buf = b""

    def open(self):
        """Open the tty in raw mode at 115200 baud and start to read it."""
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            attrs[4] = attrs[5] = termios.B115200
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            os.close(fd)
            raise
        self._fd = fd
        asyncio.get_running_loop().add_reader(fd, self._readable)

    def send(self, obj):
        """Write one line; raise OSError when the link fails. The registry limits keep each line under 1024 bytes."""
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        if os.write(self._fd, data) != len(data):
            raise OSError("short serial write")

    def close(self):
        """Stop to read and close the tty; robotd drops the link object after this."""
        asyncio.get_running_loop().remove_reader(self._fd)
        os.close(self._fd)

    def _readable(self):
        """Read the tty and pass each JSON object line on; boot text and too long lines are dropped."""
        try:
            data = os.read(self._fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            self._on_lost(f"serial read failed: {exc}")
            return
        if not data:
            self._on_lost("serial closed")
            return
        *lines, self._buf = (self._buf + data).split(b"\n")
        if len(self._buf) > ESP_LINE_LIMIT:
            self._buf = b""
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # boot text from the board
            if isinstance(msg, dict):
                self._on_message(msg)


# ---------------------------------------------------------------- daemon


class Action:
    """The one running action: its id, kind, inner task, the ESP32 ids it sent and their frames."""

    def __init__(self, aid, kind):
        self.aid = aid
        self.kind = kind
        self.task = None
        self.esp_ids = set()
        self.frames = 0
        self.reason = "stopped"


class Robotd:
    """All robotd state. Each field has one writer, and only this class opens the serial port."""

    def __init__(self, serial_path, registry_path):
        self.serial_path = serial_path
        self.registry_path = registry_path
        self._load_registry()
        self.link = None
        self.link_up = False
        self.esp_state = "down"
        self.esp_max_loop_us = None
        self.armed = False
        self.fault = None
        self.action = None
        self.loop_hz = 0.0
        self.degraded_windows = 0
        self._sid = 0
        self._esp_ids = itertools.count(1)
        self._esp_rx = 0.0
        self._hello = None
        self._acks = {}
        self._dones = {}
        self._capture = None
        self._loaded = 0
        self._move = (0.0, 0.0, 0.0)
        self._stick = None
        self._stick_latched = False  # set by a stop or disarm; cleared when the stick is back at center
        self._pose = (0.0, {})
        self._results = collections.OrderedDict()
        self._last_activity = time.monotonic()
        self._tick_errors = 0

    def handlers(self):
        """Return the method table for dogproto.serve: robot.x goes to h_x."""
        return {m: getattr(self, "h_" + m[len("robot."):]) for m in dogproto.PARAMS if m.startswith("robot.")}

    def _load_registry(self):
        """Read the registry; one that breaks a rule is replaced by an empty one, so nothing can load or move."""
        try:
            with open(self.registry_path) as handle:
                doc = json.load(handle)
            self.reg_errors = check_registry(doc)
        except (OSError, ValueError) as exc:
            self.reg_errors = [f"cannot read registry: {exc}"]
        self.registry = normalize({} if self.reg_errors else doc)
        self.caps = {cloud: capabilities(self.registry, cloud) for cloud in (False, True)}

    # ---- ESP32 messages

    def _on_esp(self, msg):
        """Apply one bridge event."""
        self._esp_rx = time.monotonic()
        ev = msg.get("ev")
        if ev == "hello":
            if msg.get("sid") != self._sid:
                if self.link_up:
                    self._link_lost("esp32 reset")
            elif self._hello is not None and not self._hello.done():
                # The handshake repeats hello, so a slow bridge can answer twice; our own sid is never a reset.
                self._hello.set_result(msg)
        elif ev in ("ack", "nack", "done"):
            future = (self._dones if ev == "done" else self._acks).get(msg.get("id"))
            if future is not None and not future.done():
                future.set_result(msg)
        elif ev == "tx":
            if self.action is not None and msg.get("id") in self.action.esp_ids:
                self.action.frames += msg.get("frames", 1)
        elif ev == "state":
            self.esp_state = msg.get("s", "?")
            self.esp_max_loop_us = msg.get("max_loop_us")
            if self.esp_state == "FAULT" and self.armed:
                self._to_fault("esp32 in FAULT")
        elif ev == "fault":
            self._to_fault(f"esp32: {msg.get('why', '?')}")
        elif ev == "cap" and self._capture is not None:
            self._capture.append({k: v for k, v in msg.items() if k != "ev"})

    def _link_lost(self, reason):
        """Close the link; an armed dog goes to FAULT."""
        log.warning("esp32 link lost: %s", reason)
        was_up = self.link_up
        self._close_link()
        if was_up and self.armed:
            self._to_fault(f"link lost: {reason}")

    def _close_link(self):
        """Close the tty and fail each op that waits for the bridge."""
        if self.link is not None:
            self.link.close()
        self.link = None
        self.link_up = False
        self.esp_state = "down"
        for future in [*self._acks.values(), *self._dones.values()]:
            if not future.done():
                future.set_exception(RpcError(dogproto.UNAVAILABLE, "esp32 link down"))
        self._acks.clear()
        self._dones.clear()

    async def _open_link(self):
        """Open the tty, repeat hello until our own reply, then load the registry into bridge RAM."""
        link = EspLink(self.serial_path, self._on_esp, self._link_lost)
        link.open()
        self.link = link
        self._sid = random.randrange(1, 2 ** 31)
        self._esp_ids = itertools.count(1)
        self._hello = asyncio.get_running_loop().create_future()
        deadline = time.monotonic() + HELLO_WAIT_S
        while not self._hello.done():
            if self.link is not link or time.monotonic() > deadline:
                raise OSError("no hello from esp32")
            link.send({"op": "hello", "sid": self._sid})
            await asyncio.wait({self._hello}, timeout=0.5)
        hello = self._hello.result()
        entries = [(name, cmd) for name, cmd in self.registry["commands"].items() if cmd["ir"] is not None]
        for name, cmd in entries:
            await self._esp_call("load", **ir_wire(name, cmd))
        self._loaded = len(entries)
        self._esp_rx = time.monotonic()
        # Report the bridge state only now: a client that sees DISARMED must be able to arm at once.
        self.esp_state = hello.get("state", "?")
        self.link_up = True
        log.info("esp32 link up: fw=%s boot=%s loaded=%d", hello.get("fw"), hello.get("boot"), self._loaded)

    async def connector(self):
        """Open and re-open the ESP32 link forever; a new link always starts DISARMED."""
        last_error = None  # log each distinct failure once, not each second
        while True:
            if self.link is None:
                try:
                    await self._open_link()
                    last_error = None
                except Exception as exc:  # any failure must leave the connector alive for the next try
                    if str(exc) != last_error:
                        last_error = str(exc)
                        log.warning("esp32 open failed: %s", exc)
                    self._close_link()
            await asyncio.sleep(1.0)

    def _esp_send(self, op, **fields):
        """Send one op with a new id, counted on the running action; return the id."""
        if self.link is None:
            raise RpcError(dogproto.UNAVAILABLE, "esp32 link down")
        esp_id = next(self._esp_ids)
        if self.action is not None:
            self.action.esp_ids.add(esp_id)
        try:
            self.link.send({"op": op, "id": esp_id, **fields})
        except OSError as exc:
            self._link_lost(str(exc))
            raise RpcError(dogproto.UNAVAILABLE, "esp32 link down") from exc
        return esp_id

    async def _esp_call(self, op, done_s=None, **fields):
        """Send one op, wait for its ack and, with done_s, for its done; raise RpcError on nack or silence.

        The reply futures exist before the next await, so a fast reply is never lost.
        """
        esp_id = self._esp_send(op, **fields)
        loop = asyncio.get_running_loop()
        ack = self._acks[esp_id] = loop.create_future()
        if done_s:
            done = self._dones[esp_id] = loop.create_future()
        try:
            # Not wait_for: on Python 3.11 it drops a cancel that arrives with the reply, so a stop could be lost.
            async with asyncio.timeout(ACK_WAIT_S):
                reply = await ack
            if reply.get("ev") == "nack":
                raise RpcError(dogproto.FAULT, f"esp32 refused {op}: {reply.get('why', '?')}")
            if done_s:
                async with asyncio.timeout(done_s):
                    await done
        except TimeoutError as exc:
            raise RpcError(dogproto.TIMEOUT, f"esp32 did not finish {op}") from exc
        finally:
            self._acks.pop(esp_id, None)
            self._dones.pop(esp_id, None)

    def _esp_quiet(self, op, **fields):
        """Send an op and ignore a link failure; used on the stop path."""
        with contextlib.suppress(RpcError):
            self._esp_send(op, **fields)

    # ---- safety state

    def _touch(self):
        """Restart the arm idle timer."""
        self._last_activity = time.monotonic()

    def _to_fault(self, reason):
        """Go to FAULT and disarm; only robot.enable clears it."""
        log.error("FAULT: %s", reason)
        self.fault = reason
        self._disarm(reason)

    def _cancel_action(self, reason):
        """Cancel the running action; return True when one was running."""
        action = self.action
        if action is None or action.task.done():
            return False
        action.reason = reason
        action.task.cancel()
        return True

    def _disarm(self, reason):
        """Disarm now; a running action first sends its stop_via, and _run_action then sends the bridge disarm."""
        if self.armed:
            log.info("disarm: %s", reason)
        self.armed = False
        self._latch_stick()
        if self._cancel_action(reason):
            self._esp_quiet("stop")  # the bridge refuses stop_via once DISARMED
        else:
            self._esp_quiet("disarm")

    def _latch_stick(self):
        """Drop the stick intent and ignore the stick until it is back at center; padd repeats it at 20 Hz."""
        self._move = (0.0, 0.0, 0.0)
        self._stick = None
        self._stick_latched = True

    def _require_link(self):
        """Raise UNAVAILABLE while the bridge link is down."""
        if not self.link_up:
            raise RpcError(dogproto.UNAVAILABLE, "esp32 link down")

    def _require_ready(self):
        """Raise unless robotd can start an action."""
        if self.fault:
            raise RpcError(dogproto.FAULT, self.fault)
        if not self.armed:
            raise RpcError(dogproto.NOT_ARMED, "not armed: ask the owner to press Start")
        self._require_link()

    def health(self):
        """Return "ok", "degraded" or "fault" for the update gate and the console."""
        if self.fault:
            return "fault"
        if not self.link_up or self.reg_errors or self.degraded_windows >= DEGRADED_WINDOWS:
            return "degraded"
        return "ok"

    # ---- loop

    async def run_loop(self):
        """Run the 20 Hz loop: watchdogs, heartbeat, deadman, arm expiry and the stick."""
        period = 1.0 / LOOP_HZ
        next_t = time.monotonic()
        tick = 0
        window_start, window_ticks = next_t, 0
        while True:
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_t = time.monotonic()  # skip missed ticks
            try:
                self.tick(tick)
                self._tick_errors = 0
            except Exception:
                log.exception("tick failed")
                self._tick_errors += 1
                if self._tick_errors >= MAX_TICK_ERRORS:
                    self._to_fault("loop errors")
            sd_notify("WATCHDOG=1")
            tick += 1
            window_ticks += 1
            now = time.monotonic()
            if now - window_start >= 1.0:
                self.loop_hz = window_ticks / (now - window_start)
                self.degraded_windows = self.degraded_windows + 1 if self.loop_hz < DEGRADED_HZ else 0
                window_start, window_ticks = now, 0

    def tick(self, tick):
        """One loop step; never awaits."""
        now = time.monotonic()
        if self.link_up and now - self._esp_rx > ESP_SILENCE_S:
            self._link_lost("esp32 silent")
        if self.link_up and tick % HEARTBEAT_TICKS == 0:
            try:
                self.link.send({"op": "hb", "seq": tick})
            except OSError as exc:
                self._link_lost(str(exc))
        if self.armed and now - self._last_activity > ARM_IDLE_S:
            self._disarm("idle")
        vx, vyaw, t_move = self._move
        if now - t_move > DEADMAN_S:
            vx = vyaw = 0.0
        self._drive_stick(stick_dir(vx, vyaw) if self.armed and self.link_up else None)

    def _drive_stick(self, direction):
        """Keep the stick command held with short leases, or release it."""
        if not (direction and self.caps[False]["drive"][direction]):
            if self._stick is not None:
                self._stick = None
                self._esp_quiet("stop")
            return
        self._touch()
        self._stick = self.registry["drive"][direction]
        if self._cancel_action("preempted by local stick"):
            return  # hold from the next tick, after the action's own stop and stop_via have gone out
        self._esp_quiet("hold", name=self._stick, lease_ms=HOLD_LEASE_MS)

    # ---- actions

    async def _run_action(self, kind, aid, body):
        """Run body() as the one action with the busy, duplicate, ownership and stop rules."""
        if aid in self._results:
            ok, value = self._results[aid]
            if ok:
                return value
            raise RpcError(*value)
        self._require_ready()
        if self.action is not None:
            raise RpcError(dogproto.BUSY, f"busy with {self.action.kind} {self.action.aid}")
        if self._stick is not None:
            raise RpcError(dogproto.BUSY, "the local stick is in control")
        before = self._robot_pose()
        action = Action(aid, kind)
        self.action = action
        self._touch()
        action.task = asyncio.ensure_future(body())
        # Every exit path records the id, so a retry with the same id never moves the dog again.
        try:
            result = await action.task
            result = {**(result or {}), "transmitted": action.frames > 0, "frames": action.frames,
                      "observed": self._observed(before)}
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                self._remember(aid, False, (dogproto.PREEMPTED, "owner disconnected"))
                raise  # the owner connection closed; the inner task was cancelled with it
            self._remember(aid, False, (dogproto.PREEMPTED, action.reason))
            raise RpcError(dogproto.PREEMPTED, action.reason) from None
        except RpcError as err:
            self._remember(aid, False, (err.code, err.message))
            raise
        except Exception as exc:
            log.exception("action %s failed", aid)
            self._remember(aid, False, (dogproto.INTERNAL_ERROR, str(exc)))
            raise RpcError(dogproto.INTERNAL_ERROR, str(exc)) from exc
        finally:
            self.action = None  # the only action: BUSY refuses a second one
            self._touch()
            if not self.armed:
                self._esp_quiet("disarm")  # after the body cleanup, which needs READY for stop_via
        self._remember(aid, True, result)
        return result

    def _remember(self, aid, ok, value):
        """Keep the result of the last RESULT_CACHE action ids."""
        self._results[aid] = (ok, value)
        while len(self._results) > RESULT_CACHE:
            self._results.popitem(last=False)

    async def _hold(self, name, ms):
        """Hold one command for ms with renewed leases, then release it."""
        end = time.monotonic() + ms / 1000
        first = True
        try:
            while (left := end - time.monotonic()) > 0:
                lease = max(1, int(min(HOLD_LEASE_MS, left * 1000)))
                if first:
                    await self._esp_call("hold", name=name, lease_ms=lease)
                    first = False
                else:
                    self._esp_send("hold", name=name, lease_ms=lease)
                await asyncio.sleep(min(HOLD_RENEW_S, left))
        finally:
            self._esp_quiet("stop")

    async def _trick(self, name, cmd):
        """Run one verified command to its natural end or its limit."""
        kind = cmd["kind"]
        if kind == "hold":
            return await self._hold(name, cmd["max_run_ms"])
        cancelled = False
        try:
            await self._esp_call("send", done_s=2.0, name=name)
            if kind == "episodic":
                await asyncio.sleep((cmd["natural_ms"] or 0) / 1000)
            elif kind == "perpetual":
                await asyncio.sleep(min(cmd["natural_ms"] or cmd["max_run_ms"], cmd["max_run_ms"]) / 1000)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # A perpetual trick always ends with its stop command; an episodic one only when stopped early.
            if cmd["stop_via"] in self.registry["commands"] and (kind == "perpetual" or cancelled):
                self._esp_quiet("send", name=cmd["stop_via"])

    # ---- vision

    def _markers(self):
        """Return (age in s, markers) of the last camera frame."""
        t_ns, markers = self._pose
        return (time.monotonic() - t_ns / 1e9 if t_ns else math.inf), markers

    def _robot_pose(self):
        """Return the robot marker (cx, cy, heading), or None when it is not seen or the frame is old."""
        age, markers = self._markers()
        robot_id = str(self.registry["goto"]["robot_marker"])
        if age > VISION_MAX_AGE_S or robot_id not in markers:
            return None
        return tuple(markers[robot_id])

    def _observed(self, before):
        """Return the camera result of an action, or None without a pose before and after."""
        after = self._robot_pose()
        if before is None or after is None:
            return None
        return {"moved_px": round(math.hypot(after[0] - before[0], after[1] - before[1]), 1),
                "turned_deg": round(math.degrees(wrap(after[2] - before[2])), 1)}

    # ---- method handlers

    async def h_move(self, params, conn):
        """robot.move: store the stick intent; the loop applies it with the deadman. dogproto refuses NaN."""
        vx = max(-1.0, min(1.0, float(params["vx"])))
        vyaw = max(-1.0, min(1.0, float(params["vyaw"])))
        if self._stick_latched:
            # After a stop or disarm, a stick that stays pushed must not move the dog again.
            if stick_dir(vx, vyaw) is not None:
                return
            self._stick_latched = False
        self._move = (vx, vyaw, time.monotonic())

    async def h_observe(self, params, conn):
        """robot.observe: store the latest marker poses from mediad; refuse a malformed frame whole."""
        markers = params["markers"]
        for value in markers.values():
            if not (isinstance(value, list) and len(value) == 3 and all(_num(v) for v in value)):
                raise RpcError(dogproto.INVALID_PARAMS, "each marker must be [cx, cy, heading_rad]")
        self._pose = (params["t_mono_ns"], markers)

    async def h_enable(self, params, conn):
        """robot.enable: arm the dog; clears a FAULT once the link is healthy again."""
        if self.reg_errors:
            raise RpcError(dogproto.UNAVAILABLE, "registry not ready", self.reg_errors[:5])
        self._require_link()
        await self._esp_call("arm", n=self._loaded)
        self._require_link()  # a bridge reset right after its ack starts a new, DISARMED link
        self.fault = None
        self.armed = True
        self._touch()
        log.info("armed")
        return self.state(conn)

    async def h_disarm(self, params, conn):
        """robot.disarm: cancel the action and disarm."""
        self._disarm("request")
        return self.state(conn)

    async def h_stop(self, params, conn):
        """robot.stop: stop IR output now and cancel the action; stay armed."""
        self._latch_stick()
        self._esp_quiet("stop")
        self._cancel_action("stopped")
        return {"stop_transmitted": self.link_up}  # read after the send: a failed write drops the link

    async def h_do(self, params, conn):
        """robot.do: run one verified trick; replies when it ends."""
        name = params["trick"]
        cmd = self.registry["commands"].get(name)
        if cmd is None or not cmd["verified"]:
            raise RpcError(dogproto.UNAVAILABLE, f"{name} is not a verified trick")
        if conn.cloud and not cmd["cloud"]:
            raise RpcError(dogproto.UNAVAILABLE, f"{name} is not enabled for remote use")
        return await self._run_action("do", params["id"], lambda: self._trick(name, cmd))

    async def h_drive(self, params, conn):
        """robot.drive: hold one drive direction for a bounded time."""
        direction, ms = params["dir"], params["ms"]
        if direction not in DIRS:
            raise RpcError(dogproto.INVALID_PARAMS, f"dir must be one of {DIRS}")
        if not self.caps[conn.cloud]["drive"][direction]:
            raise RpcError(dogproto.UNAVAILABLE, f"{direction} is not available")
        name = self.registry["drive"][direction]
        limit = self.registry["commands"][name]["max_run_ms"]
        if not 50 <= ms <= limit:
            raise RpcError(dogproto.INVALID_PARAMS, f"ms must be 50..{limit}")
        return await self._run_action("drive", params["id"], lambda: self._hold(name, ms))

    async def h_goto(self, params, conn):
        """robot.goto: drive toward an ArUco marker with short pulses, 30 s limit."""
        caps = self.caps[conn.cloud]
        if not caps["goto"]:
            raise RpcError(dogproto.UNAVAILABLE, "goto needs verified forward and turn commands")
        return await self._run_action("goto", params["id"], lambda: self._goto(str(params["marker"]), caps["drive"]))

    async def _goto(self, goal_id, caps):
        """Drive toward the goal marker; the goal is where the camera last saw it in this goto."""
        cfg = self.registry["goto"]
        drive = self.registry["drive"]
        cmds = self.registry["commands"]
        best, since_best, steps, move, goal = math.inf, 0, 0, None, None
        try:
            async with asyncio.timeout(GOTO_LIMIT_S):
                for steps in range(1, GOTO_MAX_STEPS + 1):
                    _, markers = self._markers()
                    robot = self._robot_pose()
                    goal = markers.get(goal_id, goal)  # the dog covers a floor marker as it arrives
                    if robot is None or goal is None:
                        return {"reached": False, "reason": "vision stale or marker not seen", "steps": steps}
                    if not inside(robot[:2], cfg["area_px"]):
                        return {"reached": False, "reason": "robot left the area", "steps": steps}
                    dist = math.hypot(goal[0] - robot[0], goal[1] - robot[1])
                    if dist < best - 2:
                        best, since_best = dist, 0
                    elif move == "forward":  # only a forward pulse is expected to get closer
                        since_best += 1
                        if since_best >= GOTO_NO_PROGRESS_STEPS:
                            return {"reached": False, "reason": "no progress", "steps": steps}
                    move = nav_step(robot, goal, cfg["goal_radius_px"], cfg["turn_sign"])
                    if move == "done":
                        return {"reached": True, "reason": "inside goal radius", "steps": steps}
                    if move in ("left", "right") and not caps[move]:
                        move = "right" if move == "left" else "left"  # turn the long way round
                    ms = cfg["forward_ms"] if move == "forward" else cfg["turn_ms"]
                    ms = min(ms, cmds[drive[move]]["max_run_ms"])  # a pulse never exceeds its command limit
                    if ms > 0:
                        await self._hold(drive[move], ms)
                    await asyncio.sleep(cfg["settle_ms"] / 1000)
        except TimeoutError:
            return {"reached": False, "reason": "time limit", "steps": steps}
        return {"reached": False, "reason": "step limit", "steps": steps}

    async def h_state(self, params, conn):
        """robot.state: report arm, link, health, tricks and capabilities."""
        return self.state(conn)

    def state(self, conn):
        """Return the state object; a cloud caller sees only cloud tricks and cloud capabilities."""
        age, _ = self._markers()
        tricks = [{"name": name, "kind": cmd["kind"], "say": cmd["say"], "cloud": cmd["cloud"]}
                  for name, cmd in self.registry["commands"].items()
                  if cmd["verified"] and (cmd["cloud"] or not conn.cloud)]
        return {
            "api": dogproto.API_VERSION, "health": self.health(), "armed": self.armed, "fault": self.fault,
            "busy": None if self.action is None else {"kind": self.action.kind, "id": self.action.aid},
            "stick": self._stick, "esp": self.esp_state, "esp_max_loop_us": self.esp_max_loop_us,
            "loop_hz": round(self.loop_hz, 1),
            "vision_age_ms": None if math.isinf(age) else int(age * 1000),
            "registry_ready": not self.reg_errors, "registry_errors": self.reg_errors[:5],
            "tricks": tricks, "caps": self.caps[conn.cloud],
        }

    async def h_capture(self, params, conn):
        """robot.capture: record remote frames for ms; DISARMED only, one capture at a time."""
        if self.armed or self._capture is not None:
            raise RpcError(dogproto.BUSY, "a capture needs DISARMED and no other capture")
        self._require_link()
        ms = params["ms"]
        if not 100 <= ms <= 10000:
            raise RpcError(dogproto.INVALID_PARAMS, "ms must be 100..10000")
        frames = self._capture = []
        try:
            await self._esp_call("capture", done_s=ms / 1000 + 1.0, ms=ms)
            return {"frames": frames}
        finally:
            self._capture = None

    async def h_test(self, params, conn):
        """robot.test: send any command with an IR payload for up to 1 s; READY only."""
        name, ms = params["name"], params["ms"]
        cmd = self.registry["commands"].get(name)
        if cmd is None or cmd["ir"] is None:
            raise RpcError(dogproto.UNAVAILABLE, f"no IR payload for {name}")
        if not 50 <= ms <= 1000:
            raise RpcError(dogproto.INVALID_PARAMS, "ms must be 50..1000")
        once = cmd["repeat"] == "none"
        return await self._run_action("test", f"test-{time.monotonic_ns()}", lambda: (
            self._esp_call("send", done_s=2.0, name=name) if once else self._hold(name, ms)))

    async def h_reload(self, params, conn):
        """robot.reload: read the registry again and re-load the bridge; DISARMED only."""
        if self.armed:
            raise RpcError(dogproto.BUSY, "disarm before reload")
        self._load_registry()
        self._close_link()  # the connector opens it again and loads the new entries
        return {"ready": not self.reg_errors, "errors": self.reg_errors}

    async def shutdown(self):
        """Stop IR output and disarm before the process exits."""
        self._disarm("shutdown")
        await asyncio.sleep(0.05)
        self._close_link()


def load_config(path):
    """Return the [robotd] table of dog.toml, or {} when the file is absent."""
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle).get("robotd", {})
    except FileNotFoundError:
        return {}


async def main(argv=None):
    """Start robotd: sockets, connector and loop."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.environ.get("DOG_CONFIG", "/etc/dog/dog.toml"))
    parser.add_argument("--serial")
    parser.add_argument("--registry")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    daemon = Robotd(args.serial or cfg.get("serial", "/dev/ttyUSB0"),
                    args.registry or cfg.get("registry", "/etc/dog/registry.json"))
    if daemon.reg_errors:
        log.warning("registry not ready: %s", "; ".join(daemon.reg_errors[:5]))
    os.makedirs(dogproto.run_dir(), exist_ok=True)
    servers = [
        await dogproto.serve(dogproto.sock_path("robotd"), daemon.handlers(), group="dog"),
        await dogproto.serve(dogproto.sock_path("robotd", cloud=True), daemon.handlers(), cloud=True, group="doglink"),
    ]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.ensure_future(daemon.run_loop()), asyncio.ensure_future(daemon.connector())]
    sd_notify("READY=1")
    log.info("robotd ready, api %d, serial %s", dogproto.API_VERSION, daemon.serial_path)
    await stop.wait()
    await daemon.shutdown()
    for task in tasks:
        task.cancel()
    for server in servers:
        server.close()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("DOG_LOG", "INFO"), format="%(name)s %(levelname)s %(message)s")
    asyncio.run(main(sys.argv[1:]))
