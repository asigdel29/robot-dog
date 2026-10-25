"""robotctl: command-line control of the dog on the Pi, and the only writer of the registry.

It talks to robotd over the local socket and prints JSON. From a laptop, run it
over SSH: ``ssh robot-pi robotctl state``. Exit codes: 0 success, 1 the daemon
refused, 2 the daemon is not reachable, 3 bad input. The design follows
microduck's robotctl.
"""

import asyncio
import datetime
import json
import os
import statistics

import dogproto
import robotd

#: Protocols whose frames carry a toggle bit; a replay of one fixed frame is ignored on the second press.
TOGGLE_PROTOS = ("RC5", "RC6", "RC6A", "MARANTZ")


class InputError(Exception):
    """Bad input from the person at the terminal."""


def rpc(method, params=None, timeout=10.0):
    """Make one request to robotd and return its result."""
    return asyncio.run(dogproto.call_once(dogproto.sock_path("robotd"), method, params, timeout))


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
            if ((other.get("proto"), other.get("addr"), other.get("cmd"), other.get("data"))
                    != ("NEC", first["addr"], first["cmd"], first.get("data"))):
                raise InputError("the three presses decoded differently")
        else:
            a, b = first.get("raw", []), other.get("raw", [])
            if abs(len(a) - len(b)) > 2 or any(abs(x - y) > 0.25 * max(x, y) for x, y in zip(a, b)):
                raise InputError("the three presses have different timings")
    if proto == "NEC":
        ir = {"proto": "NEC", "addr": first["addr"], "cmd": first["cmd"], "bits": first.get("bits", 32),
              "raw_us": [], "carrier_khz": 38}
        if first.get("data") is not None:
            ir["data"] = first["data"]  # the 32 bits on the air: exact replay, also for an extended address
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
