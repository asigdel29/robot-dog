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
TOP_FIELDS = {"schema", "robot", "commands", "drive", "goto"}
IR_FIELDS = {"proto", "addr", "cmd", "bits", "raw_us", "carrier_khz"}
GOTO_DEFAULTS = {"robot_marker": 1, "goal_radius_px": 40, "area_px": [[0, 0], [640, 0], [640, 480], [0, 480]],
                 "turn_ms": 200, "forward_ms": 300, "settle_ms": 300, "turn_sign": 1}
CMD_DEFAULTS = {"ir": None, "kind": "episodic", "repeat": "none", "period_ms": 108, "stop_via": "none",
                "natural_ms": None, "max_run_ms": 500, "interruptible": None, "verified": False,
                "cloud": False, "say": "", "notes": ""}


# ---------------------------------------------------------------- registry


def has_payload(ir):
    """Return True when an IR entry has enough data for the bridge to send it."""
    if not isinstance(ir, dict):
        return False
    if ir.get("proto") == "NEC":
        return isinstance(ir.get("addr"), int) and isinstance(ir.get("cmd"), int)
    if ir.get("proto") == "raw":
        raw = ir.get("raw_us")
        return isinstance(raw, list) and 0 < len(raw) <= MAX_RAW and all(isinstance(v, int) and v > 0 for v in raw)
    return False


def normalize(doc):
    """Return a copy of a registry with defaults filled in for absent fields."""
    out = dict(doc)
    out["commands"] = {name: {**CMD_DEFAULTS, **cmd} for name, cmd in doc.get("commands", {}).items()}
    out["drive"] = {**{d: None for d in DIRS}, **doc.get("drive", {})}
    out["goto"] = {**GOTO_DEFAULTS, **doc.get("goto", {})}
    return out


def _shape_errors(doc):
    """Return errors for a registry whose containers have the wrong JSON types; normalize needs none."""
    if not isinstance(doc, dict):
        return ["registry is not an object"]
    errors = [f"{key} must be an object" for key in ("commands", "drive", "goto")
              if not isinstance(doc.get(key, {}), dict)]
    if errors:
        return errors
    for name, cmd in doc.get("commands", {}).items():
        if not isinstance(cmd, dict):
            errors.append(f"{name}: must be an object")
        elif cmd.get("ir") is not None and not isinstance(cmd["ir"], dict):
            errors.append(f"{name}: ir must be an object or null")
    return errors


def _int_in(value, low, high):
    """Return True for an int (not a bool) in low..high."""
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _num(value):
    """Return True for a finite int or float (not a bool)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_registry(doc):
    """Return a list of rule violations; an empty list means robotd may arm."""
    errors = _shape_errors(doc)
    if errors:
        return errors
    errors += [f"unknown field {k}" for k in doc if k not in TOP_FIELDS]
    if doc.get("schema") != 1:
        errors.append("schema must be 1")
    for name, cmd in doc.get("commands", {}).items():
        errors += [f"{name}: unknown field {k}" for k in cmd if k not in CMD_DEFAULTS]
        if isinstance(cmd.get("ir"), dict):
            errors += [f"{name}: unknown ir field {k}" for k in cmd["ir"] if k not in IR_FIELDS]
    errors += [f"drive: unknown field {k}" for k in doc.get("drive", {}) if k not in DIRS]
    errors += [f"goto: unknown field {k}" for k in doc.get("goto", {}) if k not in GOTO_DEFAULTS]
    norm = normalize(doc)
    cmds = norm["commands"]
    for name, cmd in cmds.items():
        ir = cmd["ir"]
        if ir is not None and ir.get("proto") not in PROTOS:
            errors.append(f"{name}: protocol {ir.get('proto')} refused; only NEC and raw (no toggle bit)")
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
        if cmd["verified"] and not has_payload(ir):
            errors.append(f"{name}: verified without an IR payload")
        if cmd["kind"] == "hold" and not _int_in(cmd["max_run_ms"], 1, 1000):
            errors.append(f"{name}: hold needs max_run_ms in 1..1000")
        if cmd["kind"] == "perpetual" and cmd["verified"]:
            via = cmd["stop_via"]
            if via not in cmds or not cmds[via]["verified"]:
                errors.append(f"{name}: perpetual needs stop_via naming a verified command")
        if cmd["cloud"]:
            if not cmd["verified"]:
                errors.append(f"{name}: cloud needs verified")
            if cmd["kind"] == "mode":
                errors.append(f"{name}: a mode command can never be cloud")
            if cmd["kind"] == "episodic" and not _int_in(cmd["natural_ms"], 0, 10000):
                errors.append(f"{name}: episodic cloud needs natural_ms <= 10000")
    for direction, target in norm["drive"].items():
        if target is None:
            continue
        if target not in cmds:
            errors.append(f"drive.{direction}: no command {target}")
        elif cmds[target]["kind"] != "hold":
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
    """Return which drive directions and whether goto are usable; cloud=True also needs each cloud flag."""
    cmds = normalize(doc)["commands"]

    def usable(target):
        cmd = cmds.get(target) if target else None
        return bool(cmd and cmd["verified"] is True and (cmd["cloud"] is True or not cloud))

    drive = {d: usable(t) for d, t in normalize(doc)["drive"].items()}
    return {"drive": drive, "goto": drive["forward"] and (drive["left"] or drive["right"])}


def load_registry(path):
    """Return (normalized registry or None, errors); never raises for a bad file."""
    try:
        with open(path) as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, [f"cannot read registry: {exc}"]
    errors = check_registry(doc)
    if _shape_errors(doc):
        return None, errors
    return normalize(doc), errors


def ir_wire(name, cmd):
    """Return the load message for one command."""
    ir = cmd["ir"]
    return {"name": name, "proto": ir["proto"], "addr": ir.get("addr", 0), "cmd": ir.get("cmd", 0),
            "bits": ir.get("bits", 32), "raw": ir.get("raw_us", []), "khz": ir.get("carrier_khz", 38),
            "repeat": cmd["repeat"], "period_ms": cmd["period_ms"]}


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
    """Return "done", "left", "right" or "forward" for one goto step in image space.

    robot is (cx, cy, heading_rad) and goal is (cx, cy). Image y points down, so
    a positive heading error is clockwise on screen, which is a right turn when
    the camera looks straight down. turn_sign = -1 swaps the turns for a mirrored view.
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
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        try:
            sock.sendto(state.encode(), addr)
        except OSError:
            pass


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
        except termios.error as exc:
            os.close(fd)
            raise OSError(f"tty setup failed: {exc}") from exc  # termios.error is not an OSError
        self._fd = fd
        asyncio.get_running_loop().add_reader(fd, self._readable)

    def send(self, obj):
        """Write one line; raise OSError when the link fails."""
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        if len(data) > ESP_LINE_LIMIT:
            raise ValueError("serial line too long")
        if self._fd is None:
            raise OSError("link closed")
        written = os.write(self._fd, data)
        if written != len(data):
            raise OSError("short serial write")

    def close(self):
        """Stop to read and close the tty."""
        if self._fd is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(self._fd)
        except RuntimeError:
            pass
        os.close(self._fd)
        self._fd = None

    def _readable(self):
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
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
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
    """The one running action: its id, kind, inner task and the ESP32 ids it sent."""

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
        self.registry = self.reg_errors = self.caps = None
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

    # ---- handlers table

    def handlers(self):
        """Return the method table for dogproto.serve."""
        return {
            "robot.move": self.h_move, "robot.observe": self.h_observe, "robot.enable": self.h_enable,
            "robot.disarm": self.h_disarm, "robot.stop": self.h_stop, "robot.do": self.h_do,
            "robot.drive": self.h_drive, "robot.goto": self.h_goto, "robot.state": self.h_state,
            "robot.capture": self.h_capture, "robot.test": self.h_test, "robot.reload": self.h_reload,
        }

    @property
    def registry_ok(self):
        """True when the registry loaded and broke no rule."""
        return self.registry is not None and not self.reg_errors

    def _load_registry(self):
        """Read the registry and compute the local and cloud capabilities once."""
        self.registry, self.reg_errors = load_registry(self.registry_path)
        no_caps = {"drive": {d: False for d in DIRS}, "goto": False}
        if self.registry is None:
            self.caps = {False: no_caps, True: no_caps}
        else:
            self.caps = {False: capabilities(self.registry), True: capabilities(self.registry, cloud=True)}

    # ---- ESP32 messages

    def _on_esp(self, msg):
        self._esp_rx = time.monotonic()
        ev = msg.get("ev")
        if ev == "hello":
            if msg.get("sid") == self._sid:
                # The handshake repeats hello until a reply comes, so a slow or booting
                # bridge can answer twice; a reply with our own sid is never a reset.
                if self._hello is not None and not self._hello.done():
                    self._hello.set_result(msg)
            elif self.link_up:
                self._link_lost("esp32 reset")
        elif ev in ("ack", "nack"):
            future = self._acks.get(msg.get("id"))
            if future is not None and not future.done():
                future.set_result(msg)
        elif ev == "tx":
            if self.action is not None and msg.get("id") in self.action.esp_ids:
                self.action.frames += int(msg.get("frames", 1))
        elif ev == "done":
            future = self._dones.pop(msg.get("id"), None)
            if future is not None and not future.done():
                future.set_result(msg)
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
        log.warning("esp32 link lost: %s", reason)
        was_up = self.link_up
        self._close_link()
        if was_up and self.armed:
            self._to_fault(f"link lost: {reason}")

    def _close_link(self):
        if self.link is not None:
            self.link.close()
        self.link = None
        self.link_up = False
        self.esp_state = "down"
        for future in list(self._acks.values()) + list(self._dones.values()):
            if not future.done():
                future.set_exception(RpcError(dogproto.UNAVAILABLE, "esp32 link down"))
        self._acks.clear()
        self._dones.clear()

    async def _open_link(self):
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
        loaded = 0
        if self.registry is not None:
            for name, cmd in self.registry["commands"].items():
                if has_payload(cmd["ir"]):
                    await self._esp_call("load", **ir_wire(name, cmd))
                    loaded += 1
        self._loaded = loaded
        self._esp_rx = time.monotonic()
        # Report the bridge state only now: a client that sees DISARMED must be able to arm at once.
        self.esp_state = hello.get("state", "?")
        self.link_up = True
        log.info("esp32 link up: fw=%s boot=%s loaded=%d", hello.get("fw"), hello.get("boot"), loaded)

    async def connector(self):
        """Open and re-open the ESP32 link forever; a new link always starts DISARMED."""
        while True:
            if self.link is None:
                try:
                    await self._open_link()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # any failure must leave the connector alive for the next try
                    log.debug("esp32 open failed: %s", exc)
                    self._close_link()
            await asyncio.sleep(1.0)

    def _esp_send(self, op, want_done=False, want_ack=False, **fields):
        """Send one op with a new id and track it on the running action.

        Return (id, done future or None, ack future or None). The futures exist
        before the line leaves, so a fast reply in the same read is not lost.
        """
        if self.link is None:
            raise RpcError(dogproto.UNAVAILABLE, "esp32 link down")
        esp_id = next(self._esp_ids)
        loop = asyncio.get_running_loop()
        done = ack = None
        if want_done:
            done = self._dones[esp_id] = loop.create_future()
        if want_ack:
            ack = self._acks[esp_id] = loop.create_future()
        if self.action is not None:
            self.action.esp_ids.add(esp_id)
        try:
            self.link.send({"op": op, "id": esp_id, **fields})
        except OSError as exc:
            self._dones.pop(esp_id, None)
            self._acks.pop(esp_id, None)
            self._link_lost(str(exc))
            raise RpcError(dogproto.UNAVAILABLE, "esp32 link down") from exc
        return esp_id, done, ack

    async def _esp_call(self, op, want_done=False, **fields):
        """Send one op and wait for its ack; return (id, done future); raise RpcError on nack or silence."""
        esp_id, done, ack = self._esp_send(op, want_done=want_done, want_ack=True, **fields)
        try:
            # Not wait_for: on Python 3.11 it drops a cancel that arrives with the reply, so a stop could be lost.
            async with asyncio.timeout(ACK_WAIT_S):
                reply = await ack
        except TimeoutError as exc:
            self._dones.pop(esp_id, None)
            raise RpcError(dogproto.TIMEOUT, f"esp32 did not answer {op}") from exc
        finally:
            self._acks.pop(esp_id, None)
        if reply.get("ev") == "nack":
            self._dones.pop(esp_id, None)
            raise RpcError(dogproto.FAULT, f"esp32 refused {op}: {reply.get('why', '?')}")
        return esp_id, done

    def _esp_quiet(self, op, **fields):
        """Send an op and ignore a link failure; used on the stop path."""
        try:
            self._esp_send(op, **fields)
        except RpcError:
            pass

    # ---- safety state

    def _touch(self):
        self._last_activity = time.monotonic()

    def _to_fault(self, reason):
        log.error("FAULT: %s", reason)
        self.fault = reason
        self._disarm(reason)

    def _cancel_action(self, reason):
        """Cancel the running action; return True when one was running."""
        action = self.action
        if action is not None and action.task is not None and not action.task.done():
            action.reason = reason
            action.task.cancel()
            return True
        return False

    def _disarm(self, reason):
        """Disarm now. With an action running, only stop repeats here: the action's cleanup may still
        need to send its stop_via command, which the bridge refuses once DISARMED, and _run_action
        sends the bridge disarm after that cleanup."""
        if self.armed:
            log.info("disarm: %s", reason)
        self.armed = False
        self._latch_stick()
        if self._cancel_action(reason):
            self._esp_quiet("stop")
        else:
            self._esp_quiet("disarm")

    def _latch_stick(self):
        """Drop the stick intent and ignore stick input until the stick is back at center.

        padd sends the last stick value again at 20 Hz, so without the latch a stop
        or disarm would be undone by the next tick while the stick stays pushed.
        """
        self._move = (0.0, 0.0, 0.0)
        self._stick = None
        self._stick_latched = True

    def _require_ready(self):
        if self.fault:
            raise RpcError(dogproto.FAULT, self.fault)
        if not self.armed:
            raise RpcError(dogproto.NOT_ARMED, "not armed: ask the owner to press Start")
        if not self.link_up:
            raise RpcError(dogproto.UNAVAILABLE, "esp32 link down")

    def health(self):
        """Return "ok", "degraded" or "fault" for the update gate and the console."""
        if self.fault:
            return "fault"
        if not self.link_up or not self.registry_ok or self.degraded_windows >= DEGRADED_WINDOWS:
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
        name = None
        if direction and self.caps[False]["drive"].get(direction):
            name = self.registry["drive"][direction]
        if name is None:
            if self._stick is not None:
                self._stick = None
                self._esp_quiet("stop")
            return
        self._touch()
        self._stick = name
        if self._cancel_action("preempted by local stick"):
            return  # hold from the next tick, after the action's own stop and stop_via have gone out
        self._esp_quiet("hold", name=name, lease_ms=HOLD_LEASE_MS)

    # ---- action plumbing

    async def _run_action(self, kind, aid, conn, body):
        """Run one action body with the busy, duplicate, ownership and stop rules."""
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
        action.task = asyncio.ensure_future(body(action))
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
            if self.action is action:
                self.action = None
            self._touch()
            if not self.armed:
                self._esp_quiet("disarm")  # after the body cleanup, which needs READY for stop_via
        self._remember(aid, True, result)
        return result

    def _remember(self, aid, ok, value):
        self._results[aid] = (ok, value)
        while len(self._results) > RESULT_CACHE:
            self._results.popitem(last=False)

    def _command(self, name, conn):
        cmd = self.registry["commands"].get(name) if self.registry else None
        if cmd is None:
            raise RpcError(dogproto.UNAVAILABLE, f"no trick {name}")
        if not cmd["verified"]:
            raise RpcError(dogproto.UNAVAILABLE, f"{name} is not verified")
        if conn.cloud and not cmd["cloud"]:
            raise RpcError(dogproto.UNAVAILABLE, f"{name} is not enabled for remote use")
        return cmd

    async def _hold(self, name, ms):
        """Hold one command for ms with renewed leases, then release it."""
        end = time.monotonic() + ms / 1000
        first = True
        try:
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    break
                lease = max(1, int(min(HOLD_LEASE_MS, left * 1000)))
                if first:
                    await self._esp_call("hold", name=name, lease_ms=lease)
                    first = False
                else:
                    self._esp_send("hold", name=name, lease_ms=lease)
                await asyncio.sleep(min(HOLD_RENEW_S, left))
        finally:
            self._esp_quiet("stop")

    async def _send_once(self, name):
        """Send one command and wait until the bridge has transmitted it."""
        _, done = await self._esp_call("send", want_done=True, name=name)
        try:
            async with asyncio.timeout(2.0):
                await done
        except TimeoutError as exc:
            raise RpcError(dogproto.TIMEOUT, f"esp32 did not finish {name}") from exc

    async def _trick(self, name, cmd):
        """Run one verified command to its natural end or its limit."""
        kind = cmd["kind"]
        if kind == "hold":
            await self._hold(name, cmd["max_run_ms"])
            return {}
        cancelled = False
        try:
            await self._send_once(name)
            if kind == "episodic":
                await asyncio.sleep((cmd["natural_ms"] or 0) / 1000)
            elif kind == "perpetual":
                await asyncio.sleep(min(cmd["natural_ms"] or cmd["max_run_ms"], cmd["max_run_ms"]) / 1000)
            return {}
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # A perpetual trick always ends with its stop command; an episodic one only when stopped early.
            via = cmd["stop_via"]
            if via in self.registry["commands"] and (kind == "perpetual" or cancelled):
                self._esp_quiet("send", name=via)

    # ---- vision

    def _markers(self):
        t_ns, markers = self._pose
        age = time.monotonic() - t_ns / 1e9 if t_ns else math.inf
        return age, markers

    def _robot_pose(self):
        age, markers = self._markers()
        robot_id = str(self.registry["goto"]["robot_marker"]) if self.registry else "1"
        if age > VISION_MAX_AGE_S or robot_id not in markers:
            return None
        return tuple(markers[robot_id])

    def _observed(self, before):
        after = self._robot_pose()
        if before is None or after is None:
            return None
        return {"moved_px": round(math.hypot(after[0] - before[0], after[1] - before[1]), 1),
                "turned_deg": round(math.degrees(wrap(after[2] - before[2])), 1)}

    # ---- method handlers

    async def h_move(self, params, conn):
        """robot.move: store the stick intent; the loop applies it with the deadman."""
        if not (_num(params["vx"]) and _num(params["vyaw"])):
            raise RpcError(dogproto.INVALID_PARAMS, "vx and vyaw must be finite")
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
        if not self.registry_ok:
            raise RpcError(dogproto.UNAVAILABLE, "registry not ready", self.reg_errors[:5])
        if not self.link_up:
            raise RpcError(dogproto.UNAVAILABLE, "esp32 link down")
        await self._esp_call("arm", n=self._loaded)
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
        cmd = self._command(params["trick"], conn)
        return await self._run_action("do", params["id"], conn, lambda a: self._trick(params["trick"], cmd))

    async def h_drive(self, params, conn):
        """robot.drive: hold one drive direction for a bounded time."""
        direction, ms = params["dir"], params["ms"]
        if direction not in DIRS:
            raise RpcError(dogproto.INVALID_PARAMS, f"dir must be one of {DIRS}")
        if not self.caps[conn.cloud]["drive"][direction]:
            raise RpcError(dogproto.UNAVAILABLE, f"{direction} is not available")
        name = self.registry["drive"][direction]
        limit = self._command(name, conn)["max_run_ms"]
        if not 50 <= ms <= limit:
            raise RpcError(dogproto.INVALID_PARAMS, f"ms must be 50..{limit}")
        return await self._run_action("drive", params["id"], conn, lambda a: self._hold(name, ms))

    async def h_goto(self, params, conn):
        """robot.goto: drive toward an ArUco marker with short pulses, 30 s limit."""
        if not self.caps[conn.cloud]["goto"]:
            raise RpcError(dogproto.UNAVAILABLE, "goto needs verified forward and turn commands")
        caps = self.caps[conn.cloud]["drive"]
        return await self._run_action("goto", params["id"], conn, lambda a: self._goto(str(params["marker"]), caps))

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
        """Return the state object; a cloud caller sees only cloud tricks."""
        age, _ = self._markers()
        tricks = []
        if self.registry:
            for name, cmd in self.registry["commands"].items():
                if cmd["verified"] and (cmd["cloud"] or not conn.cloud):
                    tricks.append({"name": name, "kind": cmd["kind"], "say": cmd["say"], "cloud": cmd["cloud"]})
        return {
            "api": dogproto.API_VERSION, "health": self.health(), "armed": self.armed, "fault": self.fault,
            "busy": None if self.action is None else {"kind": self.action.kind, "id": self.action.aid},
            "stick": self._stick, "esp": self.esp_state, "esp_max_loop_us": self.esp_max_loop_us,
            "loop_hz": round(self.loop_hz, 1),
            "vision_age_ms": None if math.isinf(age) else int(age * 1000),
            "registry_ready": self.registry_ok, "registry_errors": self.reg_errors[:5],
            "tricks": tricks, "caps": self.caps[conn.cloud],
        }

    async def h_capture(self, params, conn):
        """robot.capture: record remote frames for ms; DISARMED only."""
        if self.armed:
            raise RpcError(dogproto.BUSY, "disarm before capture")
        if not self.link_up:
            raise RpcError(dogproto.UNAVAILABLE, "esp32 link down")
        ms = params["ms"]
        if not 100 <= ms <= 10000:
            raise RpcError(dogproto.INVALID_PARAMS, "ms must be 100..10000")
        self._capture = []
        try:
            _, done = await self._esp_call("capture", want_done=True, ms=ms)
            async with asyncio.timeout(ms / 1000 + 1.0):
                await done
            return {"frames": self._capture}
        finally:
            self._capture = None

    async def h_test(self, params, conn):
        """robot.test: send any command with an IR payload for up to 1 s; READY only."""
        name, ms = params["name"], params["ms"]
        cmd = self.registry["commands"].get(name) if self.registry else None
        if cmd is None or not has_payload(cmd["ir"]):
            raise RpcError(dogproto.UNAVAILABLE, f"no IR payload for {name}")
        if not 50 <= ms <= 1000:
            raise RpcError(dogproto.INVALID_PARAMS, "ms must be 50..1000")

        async def body(action):
            if cmd["repeat"] == "none":
                await self._send_once(name)
            else:
                await self._hold(name, ms)
            return {"name": name}

        return await self._run_action("test", f"test-{time.monotonic_ns()}", conn, body)

    async def h_reload(self, params, conn):
        """robot.reload: read the registry again and re-load the bridge; DISARMED only."""
        if self.armed:
            raise RpcError(dogproto.BUSY, "disarm before reload")
        self._load_registry()
        self._close_link()  # the connector opens it again and loads the new entries
        return {"ready": self.registry_ok, "errors": self.reg_errors}

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
