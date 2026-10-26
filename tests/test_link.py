"""End-to-end tests of the cloud link: linkd, the hub, the robot tools and the voice loop.

Real processes: the fake bridge and robotd (devstack Stack), linkd, the hub
(uvicorn) and stub_hermes (Hermes Agent and OpenAI). A stub mediad cloud socket
runs in a thread of this process. LinkdRules puts a fake hub in place of the
hub, to send linkd frames that the real hub never sends. Voice latency lines are
printed for docs/test-log.md.
"""

import asyncio
import base64
import functools
import json
import os
import queue
import signal
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

import websockets.sync.server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import dogproto  # noqa: E402
import linkd  # noqa: E402
from devstack import Stack, free_port  # noqa: E402

DEVICE_TOKEN = "device-secret"
TOOLS_TOKEN = "tools-secret"
HERMES_KEY = "hermes-secret"
OPENAI_KEY = "openai-secret"
FAKE_JPEG = b"\xff\xd8\xff\xe0fake jpeg\xff\xd9"


def http_json(url, body=None, headers=None):
    """Make one HTTP request and return (status, parsed JSON body or None)."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, json.loads(reply.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, None


def answers(url):
    """Return True when a GET of url answers 200; a server that is not up yet answers False."""
    try:
        return http_json(url)[0] == 200
    except OSError:
        return False


class StubMediad(threading.Thread):
    """Stands in for mediad's cloud socket: snapshot, subscribe, audio.play and audio.stop."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        #: (t_mono_ns, params) of each accepted audio.play.
        self.plays = []
        #: True plays the part of a held push-to-talk: audio.play answers BUSY.
        self.busy = False
        self.subscribers = []
        self.ready = threading.Event()
        self.loop = None
        self._halt = None

    def run(self):
        """Serve the socket until halt()."""
        asyncio.run(self._serve())

    async def _serve(self):
        """Serve the four methods on the cloud socket."""
        self.loop = asyncio.get_running_loop()
        self._halt = asyncio.Event()
        methods = ("media.snapshot", "media.subscribe", "audio.play", "audio.stop")
        handlers = {method: functools.partial(self._on_call, method) for method in methods}
        server = await dogproto.serve(self.path, handlers, cloud=True, limit=dogproto.MEDIA_LIMIT)
        self.ready.set()
        await self._halt.wait()
        server.close()

    async def _on_call(self, method, params, conn):
        """Answer one method; a play is recorded, or refused with BUSY while busy is True."""
        if method == "media.snapshot":
            return {"jpeg_b64": base64.b64encode(FAKE_JPEG).decode(), "age_ms": 12}
        if method == "media.subscribe":
            self.subscribers.append(conn)
        if method == "audio.play":
            if self.busy:
                raise dogproto.RpcError(dogproto.BUSY, "push-to-talk is down")
            self.plays.append((time.monotonic_ns(), params))
        return {}

    def notify(self, method, params):
        """Send one notification to each subscriber; callable from the test thread."""
        def send():
            """Notify each subscriber; runs on the server loop."""
            for conn in self.subscribers:
                conn.notify(method, params)
        self.loop.call_soon_threadsafe(send)

    def halt(self):
        """Stop the socket server."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._halt.set)
        self.join(5)


class LinkStack(Stack):
    """The devstack plus a stub mediad, stub_hermes, the hub and linkd; hub_ws overrides the hub URL for linkd."""

    def __init__(self, hub_ws=None, hub_env=None):
        super().__init__()
        self.hub_ws = hub_ws
        self.hub_env = hub_env or {}
        self.mediad = None

    def start(self):
        """Start every process and wait until linkd is linked."""
        super().start()
        self.mediad = StubMediad(self.sock("mediad", cloud=True))
        self.mediad.start()
        self.mediad.ready.wait(5)
        if self.hub_ws is None:
            hub_port, stub_port = free_port(), free_port()
            self.hub_url = f"http://127.0.0.1:{hub_port}"
            self.stub_url = f"http://127.0.0.1:{stub_port}"
            self.env.update({
                "DEVICE_TOKEN": DEVICE_TOKEN, "TOOLS_TOKEN": TOOLS_TOKEN, "HERMES_URL": self.stub_url,
                "HERMES_API_KEY": HERMES_KEY, "OPENAI_API_KEY": OPENAI_KEY, "OPENAI_BASE_URL": f"{self.stub_url}/v1",
                "DOG_STT_MODEL": "stub-stt", "DOG_TTS_MODEL": "stub-tts", "DOG_TTS_VOICE": "stub-voice",
                "DOG_HUB_STATE": os.path.join(self.run_dir, "hubstate"), **self.hub_env})
            self.hub_port = hub_port
            self.spawn("hub", os.path.join(ROOT, "hub", "hub.py"), "--port", str(hub_port))
            self.wait_for(lambda: answers(f"{self.hub_url}/healthz"), "hub /healthz", timeout=20)
        self.env.update({"DOG_HUB_URL": self.hub_ws or f"ws://127.0.0.1:{self.hub_port}/device",
                         "DOG_DEVICE_TOKEN": DEVICE_TOKEN})
        self.spawn("linkd", os.path.join(ROOT, "pi", "linkd.py"), "--assume-synced")
        if self.hub_ws is None:
            self.wait_for(lambda: "device dog-01 up" in self.out("hub"), "linkd link to the hub")

    def out(self, name):
        """Return the output of one process so far."""
        with open(os.path.join(self.run_dir, f"{name}.out")) as handle:
            return handle.read()

    def stop(self):
        """Stop the processes and the stub mediad."""
        super().stop()
        if self.mediad is not None:
            self.mediad.halt()

    def armed(self):
        """Return the armed flag of robotd."""
        return self.call("robot.state")["armed"]


class LinkTestCase(unittest.TestCase):
    """Starts a fresh LinkStack for each test; hub_env adds hub settings."""

    hub_env = {}

    def setUp(self):
        """Start the stack."""
        self.stack = LinkStack(hub_env=self.hub_env)
        self.addCleanup(self.stack.stop)
        self.stack.start()

    def arm(self):
        """Arm the dog from the local socket, as gamepad Start does."""
        self.assertTrue(self.stack.call("robot.enable")["armed"])


class Tools(LinkTestCase):
    """The robot tools over the Model Context Protocol, through the hub and linkd to robotd."""

    def test_silent_hub_disarms(self):
        """A frozen hub (SIGSTOP) answers no ping: linkd drops the link and disarms in about 11 s."""
        self.arm()
        hub = self.stack.procs["hub"]
        hub.send_signal(signal.SIGSTOP)
        self.addCleanup(hub.send_signal, signal.SIGCONT)
        frozen = time.monotonic()
        self.stack.wait_for(lambda: not self.stack.armed(), "disarm after a silent hub", timeout=15)
        took = time.monotonic() - frozen
        print(f"\nsilent hub to disarm: {took:.1f} s")
        self.assertLess(took, 12.5)


class FakeHub:
    """A WebSocket server in a thread that plays the hub, so a test can send linkd any frame."""

    def __init__(self):
        self.conns = queue.Queue()
        #: True closes each new connection at once, as a hub that refuses the hello.
        self.close_at_once = False
        self._done = threading.Event()
        self.server = websockets.sync.server.serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}/device"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self, conn):
        """Hand each connection to the test and keep it open until close()."""
        self.conns.put(conn)
        if self.close_at_once:
            conn.close(code=1008)
            return
        self._done.wait()

    def close(self):
        """Stop the server and end each open connection."""
        self._done.set()
        self.server.shutdown()


class LinkdRules(unittest.TestCase):
    """linkd against a fake hub: hello, bearer, STALE, cloud-only methods, concurrency and forwards."""

    def setUp(self):
        """Start the stack with linkd linked to a fake hub, and read the hello."""
        self.hub = FakeHub()
        self.addCleanup(self.hub.close)
        self.stack = LinkStack(hub_ws=self.hub.url)
        self.addCleanup(self.stack.stop)
        self.stack.start()
        self.conn = self.hub.conns.get(timeout=10)
        self.hello = json.loads(self.conn.recv(timeout=5))
        self.ids = iter(range(1, 1000))

    def request(self, method, params=None, age_ms=0):
        """Send one request as the hub does and return its id."""
        msg_id = next(self.ids)
        self.conn.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {},
                                   "sent_at": int(time.time() * 1000) - age_ms}))
        return msg_id

    def reply(self):
        """Return the next frame from linkd that is a reply."""
        while True:
            msg = json.loads(self.conn.recv(timeout=10))
            if "id" in msg:
                return msg

    def test_hello_and_bearer(self):
        """linkd sends the bearer header and then link.hello."""
        self.assertEqual(self.conn.request.headers["Authorization"], f"Bearer {DEVICE_TOKEN}")
        self.assertEqual(self.hello["method"], "link.hello")
        self.assertEqual({k: self.hello["params"][k] for k in ("api", "device")}, {"api": 1, "device": "dog-01"})

    def test_stale_rule(self):
        """An old, future, infinite or absent sent_at gets STALE; stop, disarm and audio.stop always pass."""
        self.assertTrue(self.stack.call("robot.enable")["armed"])
        for age_ms in (5000, -5000):
            self.request("robot.do", {"trick": "bark", "id": uuid.uuid4().hex}, age_ms=age_ms)
            self.assertEqual(self.reply()["error"]["code"], dogproto.STALE, age_ms)
        self.conn.send('{"jsonrpc": "2.0", "id": 98, "method": "robot.state", "params": {}, "sent_at": 1e999}')
        self.assertEqual(self.reply()["error"]["code"], dogproto.STALE, "an infinite sent_at")
        self.conn.send(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "robot.state", "params": {}}))
        self.assertEqual(self.reply()["error"]["code"], dogproto.STALE, "no sent_at")
        self.assertEqual(self.stack.wait_ops("send", 1, timeout=0.5), [])
        for method in ("robot.stop", "robot.disarm", "audio.stop"):
            self.request(method, age_ms=60000)
            self.assertIn("result", self.reply(), method)
        self.conn.send(json.dumps({"jsonrpc": "2.0", "id": 600, "method": "robot.stop", "params": {}}))
        self.assertIn("result", self.reply(), "a stop with no sent_at")

    def test_only_cloud_methods(self):
        """Arming, stick motion, a second subscription and link methods get METHOD_NOT_FOUND."""
        for method in ("robot.enable", "robot.move", "media.subscribe", "link.hello"):
            self.request(method)
            self.assertEqual(self.reply()["error"]["code"], dogproto.METHOD_NOT_FOUND, method)
        self.assertFalse(self.stack.armed())
        self.request("robot.state")
        self.assertIn("armed", self.reply()["result"])

    def test_nan_and_infinity_refused(self):
        """A frame with NaN is dropped; 1e999 in params gets INVALID_PARAMS."""
        self.conn.send('{"jsonrpc": "2.0", "id": 500, "method": "robot.state", "params": {}, "sent_at": NaN}')
        state = self.request("robot.state")
        self.assertEqual(self.reply()["id"], state, "a frame with NaN must be dropped")
        self.conn.send('{"jsonrpc": "2.0", "id": 501, "method": "robot.state", "params": {"x": 1e999}, "sent_at": %d}'
                       % (time.time() * 1000))
        reply = self.reply()
        self.assertEqual((reply["id"], reply["error"]["code"]), (501, dogproto.INVALID_PARAMS))

    def test_stop_overtakes_drive(self):
        """A stop gets its reply before the drive that it ends, which gets PREEMPTED."""
        self.assertTrue(self.stack.call("robot.enable")["armed"])
        drive = self.request("robot.drive", {"dir": "forward", "ms": 1000, "id": uuid.uuid4().hex})
        time.sleep(0.1)
        stop = self.request("robot.stop")
        first, second = self.reply(), self.reply()
        self.assertEqual((first["id"], second["id"]), (stop, drive))
        self.assertEqual(second["error"]["code"], dogproto.PREEMPTED)

    def test_forwards_utterance_and_bargein(self):
        """mediad utterances and barge-ins reach the hub as voice.utterance and voice.cancel."""
        self.stack.wait_for(lambda: self.stack.mediad.subscribers, "linkd subscribed to mediad")
        self.stack.mediad.notify("media.utterance", {"id": "u1", "rate": 16000, "pcm16_b64": ""})
        self.stack.mediad.notify("media.bargein", {})
        frames = [json.loads(self.conn.recv(timeout=5)) for _ in range(2)]
        self.assertEqual([f["method"] for f in frames], ["voice.utterance", "voice.cancel"])
        self.assertEqual(frames[0]["params"]["id"], "u1")

    def test_link_loss_disarms(self):
        """A closed link and a SIGTERM of linkd both disarm the dog; a reconnect does not arm it."""
        self.assertTrue(self.stack.call("robot.enable")["armed"])
        self.conn.close()
        self.stack.wait_for(lambda: not self.stack.armed(), "disarm after link loss")
        self.hub.conns.get(timeout=10)  # linkd reconnects
        self.assertFalse(self.stack.armed(), "a reconnect must not arm")
        self.assertTrue(self.stack.call("robot.enable")["armed"])
        self.stack.kill("linkd", signal.SIGTERM)
        self.assertFalse(self.stack.armed(), "disarm on SIGTERM")

    def test_backoff_after_short_sessions(self):
        """A hub that closes each link at once gets 1, 2, 4 s waits, not a reconnect and disarm each second."""
        self.hub.close_at_once = True
        self.conn.close()
        time.sleep(6.0)
        attempts = self.hub.conns.qsize()
        print(f"\nreconnects in 6 s against a hub that closes at once: {attempts}")
        self.assertLessEqual(attempts, 3)


class ClockWatch(unittest.TestCase):
    """linkd's NTP flag: refreshed off the event loop; an unknown state keeps the last value."""

    def test_unknown_state_keeps_last_value(self):
        """The flag follows True, keeps True on an unknown state, then goes False; False makes a request STALE."""
        link = linkd.Link("ws://127.0.0.1:1/device", "token", "dog-01", clock_synced=False)
        answers, seen = [True, None, False], []

        def fake_check():
            """Record the flag before each check and return the next answer."""
            seen.append(link.clock_synced)
            return answers.pop(0) if answers else None

        async def scenario():
            """Run the watcher until it made four checks."""
            task = asyncio.ensure_future(link._watch_clock())
            while len(seen) < 4:
                await asyncio.sleep(0.01)
            task.cancel()

        with mock.patch.object(linkd, "ntp_synced", fake_check), mock.patch.object(linkd, "CLOCK_CHECK_S", 0):
            asyncio.run(scenario())
        self.assertEqual(seen[:4], [False, True, True, False])
        now = time.time() * 1000
        with self.assertRaises(dogproto.RpcError) as ctx:
            link._check_fresh(now, now)
        self.assertEqual(ctx.exception.code, dogproto.STALE)


if __name__ == "__main__":
    unittest.main()
