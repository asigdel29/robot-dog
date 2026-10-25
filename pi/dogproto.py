"""Local bus shared by the robot dog daemons: JSON-RPC 2.0, one object per line.

Each daemon serves two unix sockets in DOG_RUN_DIR (default /run/dog): a local
socket for processes on the Pi and a cloud socket that only linkd can open.
File permissions decide who connects; the cloud socket also refuses every
method outside CLOUD_METHODS. The PARAMS table is both the validator and the
API reference. The design follows microduck's duck-ipc-proto.
"""

import asyncio
import contextlib
import itertools
import json
import logging
import math
import os
import shutil

log = logging.getLogger("dogproto")

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


class Conn:
    """One accepted client connection; handlers use it to notify and to own actions."""

    #: Notifications are dropped for a client whose unsent bytes pass this size.
    MAX_BACKLOG = 4 * 1024 * 1024

    def __init__(self, writer, cloud):
        self.cloud = cloud
        self._writer = writer
        self.on_close = []
        self.closed = False

    def send(self, obj):
        """Queue one message; a closed connection ignores it."""
        if not self.closed:
            self._writer.write(encode(obj))

    def notify(self, method, params=None):
        """Send a notification, or drop it when this client reads too slowly."""
        transport = self._writer.transport
        if transport is not None and transport.get_write_buffer_size() > self.MAX_BACKLOG:
            return
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})


async def serve(path, handlers, *, cloud=False, limit=SMALL_LIMIT, group=None):
    """Serve handlers, {method: async handler(params, conn)}, on a unix socket; return the asyncio server.

    Each request runs in its own task, so a stop can overtake a long action on the same connection.
    """
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    server = await asyncio.start_unix_server(
        lambda r, w: _client(r, w, handlers, cloud), path=path, limit=limit)
    os.chmod(path, 0o660)
    if group:
        try:
            shutil.chown(path, group=group)
        except (LookupError, PermissionError) as exc:
            # Fail closed: only this process's user may connect, whatever its primary group is.
            os.chmod(path, 0o600)
            log.warning("chown %s to group %s failed, socket is owner-only: %s", path, group, exc)
    return server


async def _client(reader, writer, handlers, cloud):
    """Serve one connection; on close, cancel its open requests and run its on_close callbacks."""
    conn = Conn(writer, cloud)
    tasks = set()
    try:
        while True:
            try:
                line = await reader.readline()
            except ValueError:  # the line passes the limit
                conn.send(_error(None, RpcError(PARSE_ERROR, "line too long")))
                break
            if not line:
                break
            task = _dispatch(line, conn, handlers)
            if task is not None:
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    except ConnectionError:
        pass
    finally:
        conn.closed = True
        for task in list(tasks):
            task.cancel()
        for callback in conn.on_close:
            try:
                callback()
            except Exception:
                log.exception("on_close callback failed")
        writer.close()


def _error(msg_id, err):
    """Return the JSON-RPC error reply for err."""
    return {"jsonrpc": "2.0", "id": msg_id, "error": err.to_obj()}


def _dispatch(line, conn, handlers):
    """Run one inbound line; return the task of a request, or None."""
    try:
        msg = decode(line)
    except ValueError:
        conn.send(_error(None, RpcError(PARSE_ERROR, "invalid json")))
        return None
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        conn.send(_error(msg.get("id") if isinstance(msg, dict) else None,
                         RpcError(INVALID_REQUEST, "not a JSON-RPC 2.0 request")))
        return None
    method = msg["method"]
    is_request = "id" in msg
    try:
        handler = handlers.get(method)
        if handler is None or (conn.cloud and method not in CLOUD_METHODS):
            raise RpcError(METHOD_NOT_FOUND, f"unknown method {method}")
        params = validate(method, msg.get("params"))
    except RpcError as err:
        if is_request:
            conn.send(_error(msg["id"], err))
        else:
            log.warning("dropped notification %s: %s", method, err.message)
        return None
    return asyncio.ensure_future(_run(handler, params, conn, msg["id"] if is_request else None, is_request))


async def _run(handler, params, conn, msg_id, is_request):
    """Run one handler and send its reply when the message is a request."""
    try:
        result = await handler(params, conn)
    except RpcError as err:
        if is_request:
            conn.send(_error(msg_id, err))
        return
    except Exception as exc:
        log.exception("handler failed")
        if is_request:
            conn.send(_error(msg_id, RpcError(INTERNAL_ERROR, str(exc))))
        return
    if is_request:
        conn.send({"jsonrpc": "2.0", "id": msg_id, "result": result})


class Client:
    """A connection to one daemon socket: requests, notifications, inbound notifications."""

    def __init__(self, reader, writer, on_notify):
        self._reader = reader
        self._writer = writer
        self._on_notify = on_notify
        self._ids = itertools.count(1)
        self._pending = {}
        self.closed = asyncio.Event()
        self._task = asyncio.ensure_future(self._read())

    @classmethod
    async def connect(cls, path, on_notify=None, limit=MEDIA_LIMIT):
        """Open a client; on_notify(method, params) receives server notifications."""
        reader, writer = await asyncio.open_unix_connection(path, limit=limit)
        return cls(reader, writer, on_notify)

    async def call(self, method, params=None, timeout=5.0):
        """Send a request and return its result; raise RpcError or TimeoutError."""
        if self.closed.is_set():
            raise ConnectionError("connection closed")
        msg_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        self._writer.write(encode({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(msg_id, None)

    def notify(self, method, params=None):
        """Send a notification; there is no reply."""
        if not self.closed.is_set():
            self._writer.write(encode({"jsonrpc": "2.0", "method": method, "params": params or {}}))

    async def close(self):
        """Close the connection and fail any open request."""
        self._writer.close()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task

    async def _read(self):
        """Route replies to their requests and notifications to on_notify, until the connection closes."""
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = decode(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" not in msg:
                    if self._on_notify is not None:
                        self._on_notify(msg["method"], msg.get("params") or {})
                    continue
                future = self._pending.get(msg.get("id"))
                if future is None or future.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    future.set_exception(RpcError(err.get("code"), err.get("message"), err.get("data")))
                else:
                    future.set_result(msg.get("result"))
        except (ConnectionError, ValueError):
            pass
        finally:
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("connection closed"))


async def call_once(path, method, params=None, timeout=5.0):
    """Open a connection, make one request, close it, and return the result."""
    client = await Client.connect(path)
    try:
        return await client.call(method, params, timeout)
    finally:
        await client.close()


async def keep_connected(path, on_connect, retry_s=1.0):
    """Keep one client connection to path open forever; on_connect(client) receives each new one.

    A restarted daemon is reached again within retry_s. Callers check client.closed before each use.
    """
    while True:
        try:
            client = await Client.connect(path)
        except OSError:
            pass
        else:
            log.info("connected to %s", path)
            on_connect(client)
            await client.closed.wait()
        await asyncio.sleep(retry_s)
