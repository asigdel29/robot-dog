"""robotctl: command-line control of the dog on the Pi, and the only writer of the registry.

It talks to robotd over the local socket and prints JSON. From a laptop, run it
over SSH: ``ssh robot-pi robotctl state``. Exit codes: 0 success, 1 the daemon
refused, 2 the daemon is not reachable, 3 bad input. The design follows
microduck's robotctl.
"""

import argparse
import asyncio
import datetime
import json
import os
import statistics
import sys
import uuid

import dogproto
import robotd

EXIT_OK, EXIT_REFUSED, EXIT_UNREACHABLE, EXIT_INPUT = 0, 1, 2, 3
TAP_MS = 3000
HOLD_MS = 4000
#: Protocols whose frames carry a toggle bit; a replay of one fixed frame is ignored on the second press.
TOGGLE_PROTOS = ("RC5", "RC6", "RC6A", "MARANTZ")


class InputError(Exception):
    """Bad input from the person at the terminal."""


def rpc(method, params=None, timeout=10.0):
    """Make one request to robotd and return its result."""
    return asyncio.run(dogproto.call_once(dogproto.sock_path("robotd"), method, params, timeout))


def out(value):
    """Print one JSON value for people and scripts."""
    print(json.dumps(value, indent=2))


# ---------------------------------------------------------------- registry file


def read_registry(path):
    """Return the raw registry document (not normalized), or a new empty one."""
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {"schema": 1, "robot": {"model": "supireo-y8359", "id": "dog-01"}, "commands": {},
                "drive": {d: None for d in robotd.DIRS}, "goto": dict(robotd.GOTO_DEFAULTS)}


def write_registry(path, doc):
    """Check the document, then replace the file atomically; refuse a document that breaks a rule."""
    errors = robotd.check_registry(doc)
    if errors:
        raise InputError("registry rules: " + "; ".join(errors))
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def reload_after_write():
    """Ask robotd to read the registry again; an armed dog keeps the old registry until a disarm."""
    try:
        return rpc("robot.reload")
    except dogproto.RpcError as err:
        if err.code != dogproto.BUSY:
            raise
        return {"written": True, "reload": "pending: disarm the dog, then run robotctl registry reload"}


def entry_from_captures(taps, hold):
    """Build a registry entry from three tap captures and one hold capture; raise InputError if they differ."""
    first = taps[0]
    proto = first.get("proto")
    if str(proto).upper() in TOGGLE_PROTOS:
        raise InputError(f"{proto} uses a toggle bit; replay is refused")
    for other in taps[1:]:
        if proto == "NEC":
            if (other.get("proto"), other.get("addr"), other.get("cmd")) != ("NEC", first["addr"], first["cmd"]):
                raise InputError("the three presses decoded differently")
        else:
            a, b = first.get("raw", []), other.get("raw", [])
            if abs(len(a) - len(b)) > 2 or any(abs(x - y) > 0.25 * max(x, y) for x, y in zip(a, b)):
                raise InputError("the three presses have different timings")
    if proto == "NEC":
        ir = {"proto": "NEC", "addr": first["addr"], "cmd": first["cmd"], "bits": first.get("bits", 32),
              "raw_us": [], "carrier_khz": 38}
    else:
        ir = {"proto": "raw", "addr": 0, "cmd": 0, "bits": 0, "raw_us": first.get("raw", []), "carrier_khz": 38}
    repeats = [f for f in hold if f.get("repeat")]
    full = [f for f in hold if not f.get("repeat")]
    gaps = [f["gap_us"] for f in hold if f.get("gap_us")]
    if repeats and proto == "NEC":
        repeat = "nec"
    elif len(full) >= 2:
        repeat = "resend"
    else:
        repeat = "none"
    period_ms = int(statistics.median(gaps) / 1000) if gaps else 108
    return {"ir": ir, "kind": "episodic" if repeat == "none" else "hold", "repeat": repeat,
            "period_ms": period_ms, "max_run_ms": 500, "verified": False, "cloud": False,
            "notes": f"captured {datetime.date.today().isoformat()}"}


def capture(name, registry_path, captures_dir):
    """Capture one remote button: three presses and one hold; write it as unverified."""
    taps = []
    for i in range(3):
        print(f"[{i + 1}/3] Press and release the '{name}' button once within {TAP_MS // 1000} s.", file=sys.stderr)
        frames = [f for f in rpc("robot.capture", {"ms": TAP_MS}, TAP_MS / 1000 + 3)["frames"] if not f.get("repeat")]
        if not frames:
            raise InputError("no IR frame seen; check the wires and aim the remote at the receiver")
        taps.append(frames[0])
    print(f"Hold the '{name}' button down for {HOLD_MS // 1000} s.", file=sys.stderr)
    hold = rpc("robot.capture", {"ms": HOLD_MS}, HOLD_MS / 1000 + 3)["frames"]
    entry = entry_from_captures(taps, hold)
    os.makedirs(captures_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    with open(os.path.join(captures_dir, f"{name}-{stamp}.json"), "w") as handle:
        json.dump({"name": name, "taps": taps, "hold": hold}, handle)
    doc = read_registry(registry_path)
    doc.setdefault("commands", {})[name] = entry
    write_registry(registry_path, doc)
    rpc("robot.reload")
    return {"name": name, **entry}


def verify(args, registry_path):
    """Record tested behavior for one command and mark it verified."""
    doc = read_registry(registry_path)
    cmd = doc.get("commands", {}).get(args.name)
    if cmd is None:
        raise InputError(f"no command {args.name}; capture it first")
    changes = {"kind": args.kind, "stop_via": args.stop_via, "natural_ms": args.natural_ms,
               "max_run_ms": args.max_run_ms, "say": args.say}
    for key, value in changes.items():
        if value is not None:
            cmd[key] = value
    if args.interruptible is not None:
        cmd["interruptible"] = args.interruptible == "yes"
    if args.cloud is not None:
        cmd["cloud"] = args.cloud == "yes"
    cmd["verified"] = True
    evidence = args.notes or "verified"
    cmd["notes"] = f"{cmd.get('notes', '')}; {datetime.date.today().isoformat()}: {evidence}".strip("; ")
    write_registry(registry_path, doc)
    return reload_after_write()


def map_drive(direction, name, registry_path):
    """Point one drive direction at a hold command, or clear it with 'none'."""
    if direction not in robotd.DIRS:
        raise InputError(f"direction must be one of {robotd.DIRS}")
    doc = read_registry(registry_path)
    doc.setdefault("drive", {})[direction] = None if name == "none" else name
    write_registry(registry_path, doc)
    return reload_after_write()


# ---------------------------------------------------------------- CLI


def build_parser():
    """Return the argument parser."""
    parser = argparse.ArgumentParser(prog="robotctl", description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.environ.get("DOG_CONFIG", "/etc/dog/dog.toml"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("state", "enable", "disarm", "stop"):
        sub.add_parser(name)
    sub.add_parser("do").add_argument("trick")
    drive = sub.add_parser("drive")
    drive.add_argument("dir", choices=robotd.DIRS)
    drive.add_argument("ms", type=int)
    sub.add_parser("goto").add_argument("marker", type=int)
    reg = sub.add_parser("registry").add_subparsers(dest="reg", required=True)
    reg.add_parser("list")
    reg.add_parser("reload")
    reg.add_parser("capture").add_argument("name")
    test = reg.add_parser("test")
    test.add_argument("name")
    test.add_argument("--ms", type=int, default=300)
    ver = reg.add_parser("verify")
    ver.add_argument("name")
    ver.add_argument("--kind", choices=robotd.KINDS)
    ver.add_argument("--stop-via")
    ver.add_argument("--natural-ms", type=int)
    ver.add_argument("--max-run-ms", type=int)
    ver.add_argument("--interruptible", choices=("yes", "no"))
    ver.add_argument("--cloud", choices=("yes", "no"))
    ver.add_argument("--say")
    ver.add_argument("--notes")
    mapping = reg.add_parser("map")
    mapping.add_argument("dir")
    mapping.add_argument("name")
    return parser


def run(argv):
    """Run one command and return its exit code."""
    args = build_parser().parse_args(argv)
    cfg = robotd.load_config(args.config)
    registry_path = cfg.get("registry", "/etc/dog/registry.json")
    captures_dir = cfg.get("captures", "/var/lib/dog/captures")
    try:
        if args.cmd in ("state", "enable", "disarm", "stop"):
            out(rpc(f"robot.{args.cmd}"))
        elif args.cmd == "do":
            out(rpc("robot.do", {"trick": args.trick, "id": uuid.uuid4().hex}, 30))
        elif args.cmd == "drive":
            out(rpc("robot.drive", {"dir": args.dir, "ms": args.ms, "id": uuid.uuid4().hex}, 10))
        elif args.cmd == "goto":
            out(rpc("robot.goto", {"marker": args.marker, "id": uuid.uuid4().hex}, 40))
        elif args.reg == "list":
            out(read_registry(registry_path).get("commands", {}))
        elif args.reg == "reload":
            out(rpc("robot.reload"))
        elif args.reg == "capture":
            out(capture(args.name, registry_path, captures_dir))
        elif args.reg == "test":
            out(rpc("robot.test", {"name": args.name, "ms": args.ms}, 10))
        elif args.reg == "verify":
            out(verify(args, registry_path))
        elif args.reg == "map":
            out(map_drive(args.dir, args.name, registry_path))
    except dogproto.RpcError as err:
        print(f"refused ({err.code}): {err.message}", file=sys.stderr)
        return EXIT_REFUSED
    except InputError as err:
        print(str(err), file=sys.stderr)
        return EXIT_INPUT
    except (OSError, TimeoutError) as err:  # ConnectionError, FileNotFoundError and PermissionError are OSErrors
        print(f"robotd not reachable: {err}", file=sys.stderr)
        return EXIT_UNREACHABLE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
