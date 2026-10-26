"""robotd: the only process that talks to the ESP32 IR bridge.

It owns the 20 Hz loop, the trick registry, the arm state, the one running
action and the safety rules. It serves robotd.sock (local clients) and
robotd-cloud.sock (linkd only) and never opens an outbound connection, so the
loop never waits on another service. The design follows microduck's robotd;
docs/design/safety.md and docs/design/registry.md own the rules.
"""

import math

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
