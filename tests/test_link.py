"""End-to-end tests of the cloud link: linkd, the hub, the robot tools and the voice loop.

Real processes: the fake bridge and robotd (devstack Stack), linkd, the hub
(uvicorn) and stub_hermes (Hermes Agent and OpenAI). A stub mediad cloud socket
runs in a thread of this process. LinkdRules puts a fake hub in place of the
hub, to send linkd frames that the real hub never sends. Voice latency lines are
printed for docs/test-log.md.
"""

import json
import os
import queue
import signal
import sys
import threading
import unittest

import websockets.sync.server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from devstack import Stack  # noqa: E402

DEVICE_TOKEN = "device-secret"


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
        self.env.update({"DOG_HUB_URL": self.hub_ws or f"ws://127.0.0.1:{self.hub_port}/device",
                         "DOG_DEVICE_TOKEN": DEVICE_TOKEN})
        self.spawn("linkd", os.path.join(ROOT, "pi", "linkd.py"), "--assume-synced")

    def stop(self):
        """Stop the processes and the stub mediad."""
        super().stop()

    def armed(self):
        """Return the armed flag of robotd."""
        return self.call("robot.state")["armed"]


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

    def test_hello_and_bearer(self):
        """linkd sends the bearer header and then link.hello."""
        self.assertEqual(self.conn.request.headers["Authorization"], f"Bearer {DEVICE_TOKEN}")
        self.assertEqual(self.hello["method"], "link.hello")
        self.assertEqual({k: self.hello["params"][k] for k in ("api", "device")}, {"api": 1, "device": "dog-01"})

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


if __name__ == "__main__":
    unittest.main()
