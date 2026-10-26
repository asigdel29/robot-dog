"""Local bus shared by the robot dog daemons: JSON-RPC 2.0, one object per line.

Each daemon serves two unix sockets in DOG_RUN_DIR (default /run/dog): a local
socket for processes on the Pi and a cloud socket that only linkd can open.
File permissions decide who connects; the cloud socket also refuses every
method outside CLOUD_METHODS. The PARAMS table is both the validator and the
API reference. The design follows microduck's duck-ipc-proto.
"""

import json
import math
import os

#: Bumped on any wire change. A peer with another value is logged, not refused.
API_VERSION = 1

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_ARMED = -32001
BUSY = -32002
FAULT = -32003
UNAVAILABLE = -32004
STALE = -32005
TIMEOUT = -32006
PREEMPTED = -32007

_NUM = (int, float)

#: Method name to its fields. A field name that ends in "?" is optional.
PARAMS = {
    # robotd
    "robot.move": {"vx": _NUM, "vyaw": _NUM},
    "robot.observe": {"t_mono_ns": int, "markers": dict},
    "robot.enable": {},
    "robot.disarm": {},
    "robot.stop": {},
    "robot.do": {"trick": str, "id": str},
    "robot.drive": {"dir": str, "ms": int, "id": str},
    "robot.goto": {"marker": int, "id": str},
    "robot.state": {},
    "robot.capture": {"ms": int},
    "robot.test": {"name": str, "ms": int},
    "robot.reload": {},
    # mediad
    "media.snapshot": {"max_px?": int},
    "media.subscribe": {},
    "media.utterance": {"id": str, "rate": int, "pcm16_b64": str},
    "media.bargein": {},
    "audio.play": {"pcm16_b64": str, "rate?": int},
    "audio.stop": {},
    "audio.ptt": {"down": bool},
    # linkd <-> hub
    "link.hello": {"api": int, "version": str, "device": str},
    "voice.utterance": {"id": str, "rate": int, "pcm16_b64": str},
    "voice.cancel": {"id?": str},
}

#: The only methods a cloud socket accepts. Arming and stick motion stay local.
CLOUD_METHODS = frozenset({
    "robot.disarm", "robot.stop", "robot.do", "robot.drive", "robot.goto", "robot.state",
    "media.snapshot", "media.subscribe", "audio.play", "audio.stop",
})

#: Line limits in bytes: robotd carries small control lines, mediad and linkd carry audio.
SMALL_LIMIT = 64 * 1024
MEDIA_LIMIT = 2 * 1024 * 1024


class RpcError(Exception):
    """A JSON-RPC error with a code from this module, sent back to the caller."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_obj(self):
        """Return the JSON-RPC error object."""
        err = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


def run_dir():
    """Return the socket directory; DOG_RUN_DIR overrides /run/dog for tests and dev."""
    return os.environ.get("DOG_RUN_DIR", "/run/dog")


def sock_path(service, cloud=False):
    """Return the socket path of a daemon, for example robotd or robotd-cloud."""
    suffix = "-cloud" if cloud else ""
    return os.path.join(run_dir(), f"{service}{suffix}.sock")


def _type_ok(value, expected):
    """Return True when value has the expected type; a bool is no number, and a number must be finite."""
    if isinstance(value, bool) and expected is not bool:
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return isinstance(value, expected)


def _no_constant(name):
    """Refuse NaN and Infinity in json.loads."""
    raise ValueError(f"{name} is not valid JSON")


def decode(line):
    """Parse one wire line; NaN and Infinity are refused, because a clamp turns NaN into full motion."""
    return json.loads(line, parse_constant=_no_constant)


def validate(method, params):
    """Check params against PARAMS and return them; raise RpcError on any mismatch."""
    spec = PARAMS.get(method)
    if spec is None:
        raise RpcError(METHOD_NOT_FOUND, f"unknown method {method}")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    fields = {name.rstrip("?"): (kind, not name.endswith("?")) for name, kind in spec.items()}
    for name in params:
        if name not in fields:
            raise RpcError(INVALID_PARAMS, f"unknown field {name}")
    for name, (kind, required) in fields.items():
        if name not in params:
            if required:
                raise RpcError(INVALID_PARAMS, f"missing field {name}")
            continue
        if not _type_ok(params[name], kind):
            raise RpcError(INVALID_PARAMS, f"bad type for {name}")
    return params


def encode(obj):
    """Return one wire line for obj."""
    return (json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n").encode()
