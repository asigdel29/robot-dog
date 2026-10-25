"""End-to-end tests of the cloud link: linkd, the hub, the robot tools and the voice loop.

Real processes: the fake bridge and robotd (devstack Stack), linkd, the hub
(uvicorn) and stub_hermes (Hermes Agent and OpenAI). A stub mediad cloud socket
runs in a thread of this process. Some tests put a fake hub in place of the hub,
to send linkd frames that the real hub never sends. Each check maps to a row of
the verification table in the plan. Voice latency lines are printed for docs/test-log.md.
"""

import asyncio
import base64
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid

import httpx2
import websockets
import websockets.sync.client
import websockets.sync.server
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import dogproto  # noqa: E402
from devstack import Stack  # noqa: E402
from test_e2e import PoseFeeder  # noqa: E402

DEVICE_TOKEN = "device-secret"
TOOLS_TOKEN = "tools-secret"
HERMES_KEY = "hermes-secret"
OPENAI_KEY = "openai-secret"
TOOL_NAMES = {"get_state", "do_trick", "drive", "go_to_marker", "look", "say", "stop"}
FAKE_JPEG = b"\xff\xd8\xff\xe0fake jpeg\xff\xd9"


def free_port():
    """Return a TCP port that is free now on loopback."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
    """Stands in for mediad's cloud socket: snapshot, subscribe, audio.play and audio.stop; it records each play."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        self.plays = []
        self.subscribers = []
        self.ready = threading.Event()
        self.loop = None
        self._halt = None

    def run(self):
        asyncio.run(self._serve())

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self._halt = asyncio.Event()

        async def snapshot(params, conn):
            return {"jpeg_b64": base64.b64encode(FAKE_JPEG).decode(), "age_ms": 12}

        async def subscribe(params, conn):
            self.subscribers.append(conn)
            return {}

        async def play(params, conn):
            self.plays.append((time.monotonic_ns(), params))
            return {"queued": True}

        async def audio_stop(params, conn):
            return {}

        handlers = {"media.snapshot": snapshot, "media.subscribe": subscribe, "audio.play": play,
                    "audio.stop": audio_stop}
        server = await dogproto.serve(self.path, handlers, cloud=True, limit=dogproto.MEDIA_LIMIT)
        self.ready.set()
        await self._halt.wait()
        server.close()

    def notify(self, method, params):
        """Send one notification to each subscriber; callable from the test thread."""
        def send():
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
            self.spawn("stub_hermes", os.path.join(ROOT, "sim", "stub_hermes.py"), "--port", str(stub_port),
                       "--tools-url", f"{self.hub_url}/tools/")
            self.wait_for(lambda: answers(f"{self.stub_url}/_stub/requests"), "stub_hermes", timeout=20)
            self.start_hub(hub_port)
        self.env.update({"DOG_HUB_URL": self.hub_ws or f"ws://127.0.0.1:{self.hub_port}/device",
                         "DOG_DEVICE_TOKEN": DEVICE_TOKEN})
        self.spawn("linkd", os.path.join(ROOT, "pi", "linkd.py"), "--assume-synced")
        if self.hub_ws is None:
            self.wait_for(lambda: "device dog-01 up" in self.out("hub"), "linkd link to the hub")

    def start_hub(self, port):
        """Start the hub and wait for /healthz."""
        self.hub_port = port
        self.spawn("hub", os.path.join(ROOT, "hub", "hub.py"), "--port", str(port))
        self.wait_for(lambda: answers(f"{self.hub_url}/healthz"), "hub /healthz", timeout=20)

    def out(self, name):
        """Return the output of one process so far."""
        with open(os.path.join(self.run_dir, f"{name}.out")) as handle:
            return handle.read()

    def stop(self):
        """Stop the processes and the stub mediad."""
        super().stop()
        if self.mediad is not None:
            self.mediad.halt()

    def stub_config(self, **changes):
        """Change the stub_hermes transcript or reply."""
        http_json(f"{self.stub_url}/_stub/config", changes)

    def stub_requests(self, path=None):
        """Return the requests that stub_hermes recorded, for one path or all."""
        rows = http_json(f"{self.stub_url}/_stub/requests")[1]
        return [r for r in rows if path is None or r["path"] == path]

    def tools(self, work, token=TOOLS_TOKEN):
        """Run work(client) with a Model Context Protocol client on /tools, as Hermes does."""
        async def session():
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(10, read=60)) as http:
                async with Client(streamable_http_client(f"{self.hub_url}/tools/", http_client=http)) as client:
                    return await work(client)
        return asyncio.run(session())

    def tool(self, name, arguments=None):
        """Call one tool and return (is_error, text of the result)."""
        result = self.tools(lambda client: client.call_tool(name, arguments or {}))
        return result.is_error, " ".join(getattr(block, "text", "") for block in result.content)

    def utter(self, seconds=0.5):
        """Make the stub mediad send one utterance of silence; return its id."""
        utt_id = uuid.uuid4().hex
        pcm = b"\x00\x00" * int(16000 * seconds)
        self.mediad.notify("media.utterance", {"id": utt_id, "rate": 16000,
                                               "pcm16_b64": base64.b64encode(pcm).decode()})
        return utt_id

    def latency_lines(self):
        """Return the voice latency lines of the hub log."""
        return re.findall(r"voice \S+ latency ms: .*", self.out("hub"))


class LinkTestCase(unittest.TestCase):
    """Starts a fresh LinkStack for each test; hub_env adds hub settings."""

    hub_env = {}

    def setUp(self):
        self.stack = LinkStack(hub_env=self.hub_env)
        self.addCleanup(self.stack.stop)
        self.stack.start()

    def arm(self):
        self.assertTrue(self.stack.call("robot.enable")["armed"])


class Tools(LinkTestCase):
    """The robot tools over the Model Context Protocol, through the hub and linkd to robotd."""

    def test_tool_list_and_tokens(self):
        listed = self.stack.tools(lambda client: client.list_tools())
        self.assertEqual({tool.name for tool in listed.tools}, TOOL_NAMES)
        self.assertEqual(http_json(f"{self.stack.hub_url}/tools/", {})[0], 401)
        self.assertEqual(http_json(f"{self.stack.hub_url}/tools/", {}, {"Authorization": "Bearer wrong"})[0], 401)
        device_url = f"ws://127.0.0.1:{self.stack.hub_port}/device"
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            with self.assertRaises(websockets.exceptions.InvalidStatus):
                websockets.sync.client.connect(device_url, additional_headers=headers, open_timeout=5)

    def test_do_trick_reaches_bridge(self):
        self.arm()
        error, text = self.stack.tool("do_trick", {"name": "bark"})
        self.assertFalse(error, text)
        self.assertTrue(json.loads(text)["transmitted"])
        self.assertEqual([m["name"] for _, m in self.stack.wait_ops("send", 1)], ["bark"])

    def test_drive_needs_local_arm(self):
        error, text = self.stack.tool("drive", {"direction": "forward", "ms": 300})
        self.assertTrue(error)
        self.assertIn("Start", text)
        self.assertEqual(self.stack.ops("hold"), [])
        self.arm()
        error, text = self.stack.tool("drive", {"direction": "forward", "ms": 300})
        self.assertFalse(error, text)
        self.assertTrue(json.loads(text)["transmitted"])
        self.assertTrue(self.stack.wait_ops("hold", 1))

    def test_state_look_say_stop(self):
        error, text = self.stack.tool("get_state")
        state = json.loads(text)
        self.assertEqual(state["link"], "up")
        self.assertIn("bark", {t["name"] for t in state["tricks"]})
        self.assertTrue(state["caps"]["goto"])
        result = self.stack.tools(lambda client: client.call_tool("look", {}))
        self.assertEqual(result.content[0].mime_type, "image/jpeg")
        self.assertEqual(base64.b64decode(result.content[0].data), FAKE_JPEG)
        self.assertFalse(self.stack.tool("say", {"text": "Woof."})[0])
        self.stack.wait_for(lambda: len(self.stack.mediad.plays) == 1, "audio.play from say")
        self.assertEqual(self.stack.mediad.plays[0][1]["rate"], 24000)
        self.assertTrue(self.stack.tool("say", {"text": "x" * 301})[0])
        error, text = self.stack.tool("stop")
        self.assertFalse(error, text)
        self.assertTrue(json.loads(text)["stop_transmitted"])

    def test_motion_rate_limit(self):
        async def calls(client):
            results = []
            for _ in range(31):
                result = await client.call_tool("drive", {"direction": "forward", "ms": 300})
                results.append(" ".join(block.text for block in result.content))
            return results

        texts = self.stack.tools(calls)
        self.assertTrue(all("Start" in text for text in texts[:30]), texts[:30])
        self.assertIn("rate limit", texts[30])

    def test_device_offline(self):
        self.stack.kill("linkd")
        self.stack.wait_for(lambda: "link down" in self.stack.out("hub"), "hub sees the link down")
        self.assertEqual(json.loads(self.stack.tool("get_state")[1]), {"link": "down"})
        error, text = self.stack.tool("do_trick", {"name": "bark"})
        self.assertTrue(error)
        self.assertIn("offline", text)

    def test_hub_loss_disarms_and_cancels_goto(self):
        """Hub killed during a goto: linkd disarms robotd and the goto ends; no hold follows."""
        self.arm()
        feeder = PoseFeeder(self.stack, (600, 440))
        feeder.start()
        self.addCleanup(feeder.join)
        self.addCleanup(feeder.halt.set)
        time.sleep(0.3)
        goto = threading.Thread(target=lambda: self._quiet_tool("go_to_marker", {"marker_id": 2}), daemon=True)
        goto.start()
        self.stack.wait_ops("hold", 1, timeout=5)
        killed = time.monotonic()
        self.stack.kill("hub")
        self.stack.wait_for(lambda: not self.stack.call("robot.state")["armed"], "disarm after hub loss")
        print(f"\nhub loss to disarm: {(time.monotonic() - killed) * 1000:.0f} ms")
        self.assertIsNone(self.stack.call("robot.state")["busy"])
        quiet_from = time.monotonic_ns() + 400_000_000  # one hold lease after the disarm
        time.sleep(1.0)
        self.assertEqual(self.stack.ops("hold", since_ns=quiet_from), [])
        goto.join(10)

    def _quiet_tool(self, name, arguments):
        try:
            self.stack.tool(name, arguments)
        except Exception:
            pass  # the hub dies during this call


class VoiceLoop(LinkTestCase):
    """Utterance to STT, Hermes run, TTS and audio.play, with the stop fast path and barge-in."""

    def wait_latency_line(self):
        self.stack.wait_for(lambda: self.stack.latency_lines(), "voice latency line")
        for line in self.stack.latency_lines():
            print(f"\n{line}")

    def test_utterance_plays_reply(self):
        utt_id = self.stack.utter()
        self.stack.wait_for(lambda: len(self.stack.mediad.plays) >= 2, "two sentences played")
        stt = self.stack.stub_requests("/v1/audio/transcriptions")[0]
        self.assertEqual((stt["model"], stt["wav"], stt["authorization"]), ("stub-stt", True, f"Bearer {OPENAI_KEY}"))
        run = self.stack.stub_requests("/v1/runs")[0]
        self.assertEqual(run["body"]["input"], "[spoken via dog mic] hello dog")
        self.assertEqual(run["headers"], {"authorization": f"Bearer {HERMES_KEY}", "idempotency-key": utt_id,
                                          "x-hermes-session-key": "dog-01-voice"})
        spoken = [r["body"] for r in self.stack.stub_requests("/v1/audio/speech")]
        self.assertEqual([b["input"] for b in spoken], ["Sure, I can do that.", "Here I go!"])
        self.assertEqual({(b["model"], b["voice"], b["response_format"]) for b in spoken},
                         {("stub-tts", "stub-voice", "pcm")})
        self.wait_latency_line()

    def test_tool_call_during_stream(self):
        """Hermes calls do_trick through /tools while the voice loop waits on its stream: no deadlock."""
        self.arm()
        self.stack.stub_config(transcript="please bark")
        self.stack.utter()
        self.stack.wait_for(lambda: len(self.stack.mediad.plays) >= 2, "reply played after the tool call")
        self.assertEqual([m["name"] for _, m in self.stack.wait_ops("send", 1)], ["bark"])
        call = self.stack.stub_requests("tool:do_trick")[0]
        self.assertFalse(call["is_error"], call["text"])
        self.assertTrue(json.loads(call["text"])["transmitted"])
        self.wait_latency_line()

    def test_bargein_stops_run(self):
        self.stack.stub_config(reply=[" One."] * 40, delta_delay=0.1)
        self.stack.utter()
        self.stack.wait_for(lambda: self.stack.mediad.plays, "first sentence played")
        self.stack.mediad.notify("media.bargein", {})
        self.stack.wait_for(lambda: self.stack.stub_requests("/v1/runs/stop"), "run stop at Hermes")
        stop = self.stack.stub_requests("/v1/runs/stop")[0]
        self.assertEqual(stop["run_id"], self.stack.stub_requests("/v1/runs")[0]["run_id"])
        self.assertEqual(stop["headers"]["authorization"], f"Bearer {HERMES_KEY}")
        time.sleep(0.3)
        played = len(self.stack.mediad.plays)
        time.sleep(1.0)
        self.assertEqual(len(self.stack.mediad.plays), played, "audio.play after barge-in")
        self.assertLess(played, 10)

    def test_stop_phrase_before_hermes(self):
        self.stack.stub_config(transcript="Stop! Stop right there.")
        self.stack.utter()
        self.stack.wait_for(lambda: self.stack.stub_requests("/v1/runs"), "Hermes run")
        run_ns = self.stack.stub_requests("/v1/runs")[0]["t_ns"]
        stops = self.stack.wait_ops("stop", 1)
        self.assertTrue(stops, "robot.stop did not reach the bridge")
        self.assertLess(stops[0][0], run_ns)


class DailyCaps(LinkTestCase):
    """Over a daily cap the dog says nothing new, and the hub logs it."""

    hub_env = {"DOG_TTS_CHARS_PER_DAY": "10", "DOG_STT_MINUTES_PER_DAY": "0.001"}

    def test_caps(self):
        self.assertFalse(self.stack.tool("say", {"text": "Woof."})[0])
        error, text = self.stack.tool("say", {"text": "Woof, woof."})
        self.assertTrue(error)
        self.assertIn("daily speech limit", text)
        with open(os.path.join(self.stack.run_dir, "hubstate", "usage.json")) as handle:
            self.assertEqual(json.load(handle)["tts_chars"], 5)
        self.stack.utter()
        self.stack.wait_for(lambda: "daily cap reached: stt_s" in self.stack.out("hub"), "STT cap in the hub log")
        self.assertEqual(self.stack.stub_requests("/v1/audio/transcriptions"), [])
        self.assertEqual(len(self.stack.mediad.plays), 1)


class FakeHub:
    """A WebSocket server in a thread that plays the hub, so a test can send linkd any frame."""

    def __init__(self):
        self.conns = queue.Queue()
        self._done = threading.Event()
        self.server = websockets.sync.server.serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}/device"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self, conn):
        self.conns.put(conn)
        self._done.wait()

    def close(self):
        """Stop the server and end each open connection."""
        self._done.set()
        self.server.shutdown()


class LinkdRules(unittest.TestCase):
    """linkd against a fake hub: hello, bearer, STALE, cloud-only methods, concurrency and forwards."""

    def setUp(self):
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
        self.assertEqual(self.conn.request.headers["Authorization"], f"Bearer {DEVICE_TOKEN}")
        self.assertEqual(self.hello["method"], "link.hello")
        self.assertEqual({k: self.hello["params"][k] for k in ("api", "device")}, {"api": 1, "device": "dog-01"})

    def test_old_sent_at_is_stale(self):
        self.assertTrue(self.stack.call("robot.enable")["armed"])
        self.request("robot.do", {"trick": "bark", "id": uuid.uuid4().hex}, age_ms=5000)
        self.assertEqual(self.reply()["error"]["code"], dogproto.STALE)
        self.conn.send(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "robot.state", "params": {}}))
        self.assertEqual(self.reply()["error"]["code"], dogproto.STALE)
        self.assertEqual(self.stack.wait_ops("send", 1, timeout=0.5), [])

    def test_only_cloud_methods(self):
        for method in ("robot.enable", "robot.move", "media.subscribe", "link.hello"):
            self.request(method)
            self.assertEqual(self.reply()["error"]["code"], dogproto.METHOD_NOT_FOUND, method)
        self.assertFalse(self.stack.call("robot.state")["armed"])
        self.request("robot.state")
        self.assertIn("armed", self.reply()["result"])

    def test_stop_overtakes_drive(self):
        self.assertTrue(self.stack.call("robot.enable")["armed"])
        drive = self.request("robot.drive", {"dir": "forward", "ms": 1000, "id": uuid.uuid4().hex})
        time.sleep(0.1)
        stop = self.request("robot.stop")
        first, second = self.reply(), self.reply()
        self.assertEqual((first["id"], second["id"]), (stop, drive))
        self.assertEqual(second["error"]["code"], dogproto.PREEMPTED)

    def test_forwards_utterance_and_bargein(self):
        self.stack.wait_for(lambda: self.stack.mediad.subscribers, "linkd subscribed to mediad")
        self.stack.mediad.notify("media.utterance", {"id": "u1", "rate": 16000, "pcm16_b64": ""})
        self.stack.mediad.notify("media.bargein", {})
        frames = [json.loads(self.conn.recv(timeout=5)) for _ in range(2)]
        self.assertEqual([f["method"] for f in frames], ["voice.utterance", "voice.cancel"])
        self.assertEqual(frames[0]["params"]["id"], "u1")

    def test_link_loss_disarms(self):
        self.assertTrue(self.stack.call("robot.enable")["armed"])
        self.conn.close()
        self.stack.wait_for(lambda: not self.stack.call("robot.state")["armed"], "disarm after link loss")
        self.hub.conns.get(timeout=10)  # linkd reconnects
        self.assertFalse(self.stack.call("robot.state")["armed"], "a reconnect must not arm")


if __name__ == "__main__":
    unittest.main()
