"""Serial protocol conformance for the IR bridge.

The same tests run against sim/fake_esp32.py (default) and against the real
Feather when DOG_SERIAL names its port, which keeps the fake honest. The board
needs no IR target: the tests load a dummy NEC entry.
"""

import json
import os
import select
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = {"name": "t", "proto": "NEC", "addr": 0, "cmd": 1, "bits": 32, "raw": [], "khz": 38,
         "repeat": "nec", "period_ms": 108}
_fake = None
#: Timing slack: tight for the real board, wider for the fake, which shares a busy host.
SLACK_S = 0.15 if os.environ.get("DOG_SERIAL") else 0.4


def setUpModule():
    global _fake
    if os.environ.get("DOG_SERIAL"):
        return
    run_dir = tempfile.mkdtemp(prefix="dog-conf-")
    _fake = subprocess.Popen([sys.executable, os.path.join(ROOT, "sim", "fake_esp32.py"), "--run-dir", run_dir],
                             stdout=subprocess.PIPE, text=True)
    os.environ["DOG_CONF_PORT"] = _fake.stdout.readline().split()[1]


def tearDownModule():
    if _fake is not None:
        _fake.terminate()
        _fake.wait(5)


class Port:
    """Raw access to the bridge, as robotd would see it."""

    def __init__(self):
        path = os.environ.get("DOG_SERIAL") or os.environ["DOG_CONF_PORT"]
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIFLUSH)
        self.buf = b""
        self.ids = iter(range(1, 1_000_000))

    def close(self):
        os.close(self.fd)

    def send(self, **msg):
        os.write(self.fd, (json.dumps(msg) + "\n").encode())

    def op(self, op, **fields):
        """Send an op with the next id and return the id."""
        op_id = next(self.ids)
        self.send(op=op, id=op_id, **fields)
        return op_id

    def read(self, timeout):
        """Return the next event, or None after timeout seconds."""
        end = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                return None
            self.buf += os.read(self.fd, 4096)
        line, self.buf = self.buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError:
            return self.read(max(0.0, end - time.monotonic()))

    def expect(self, ev, timeout=1.0, op_id=None):
        """Read until an event of kind ev (and id) arrives; return it or fail."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            msg = self.read(end - time.monotonic())
            if msg and msg.get("ev") == ev and (op_id is None or msg.get("id") == op_id):
                return msg
        raise AssertionError(f"no {ev} (id {op_id}) within {timeout} s")

    def hello(self):
        """Start a new session and return the hello reply; retry while a real board boots."""
        sid = int(time.time() * 1000) % 2_000_000_000
        end = time.monotonic() + 6
        while time.monotonic() < end:
            self.send(op="hello", sid=sid)
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                msg = self.read(deadline - time.monotonic())
                if msg and msg.get("ev") == "hello" and msg.get("sid") == sid:
                    self.ids = iter(range(1, 1_000_000))
                    return msg
        raise AssertionError("no hello reply")

    def ready(self):
        """hello, load one entry and arm; return when READY."""
        self.hello()
        self.expect("ack", op_id=self.op("load", **ENTRY))
        self.expect("ack", op_id=self.op("arm", n=1))

    def state(self):
        self.send(op="hb", seq=1)
        return self.expect("state")


class Conformance(unittest.TestCase):
    """Firmware rules from docs/design/protocol.md."""

    def setUp(self):
        self.port = Port()

    def tearDown(self):
        self.port.send(op="disarm", id=999_999)
        self.port.close()

    def test_hello_starts_disarmed(self):
        self.assertEqual(self.port.hello()["state"], "DISARMED")

    def test_old_id_refused(self):
        self.port.hello()
        self.port.send(op="load", id=5, **ENTRY)
        self.port.expect("ack", op_id=5)
        for old in (5, 4):
            self.port.send(op="load", id=old, **ENTRY)
            self.assertEqual(self.port.expect("nack", op_id=old)["why"], "old id")

    def test_arm_checks_entry_count(self):
        self.port.hello()
        self.port.expect("ack", op_id=self.port.op("load", **ENTRY))
        self.port.expect("nack", op_id=self.port.op("arm", n=2))
        self.port.expect("ack", op_id=self.port.op("arm", n=1))
        self.assertEqual(self.port.state()["s"], "READY")

    def test_send_refused_when_disarmed(self):
        self.port.hello()
        self.port.expect("ack", op_id=self.port.op("load", **ENTRY))
        self.port.expect("nack", op_id=self.port.op("send", name="t"))

    def test_send_transmits_once(self):
        self.port.ready()
        op_id = self.port.op("send", name="t")
        self.port.expect("ack", op_id=op_id)
        self.assertEqual(self.port.expect("tx", op_id=op_id)["frames"], 1)
        self.port.expect("done", op_id=op_id)

    def test_lease_expiry_stops_repeats(self):
        self.port.ready()
        op_id = self.port.op("hold", name="t", lease_ms=300)
        sent = time.monotonic()
        self.port.expect("ack", op_id=op_id)
        self.port.expect("done", op_id=op_id, timeout=1.0)
        self.assertLess(time.monotonic() - sent, 0.3 + SLACK_S)
        self.assertIsNone(self._next_tx(0.3), "repeat frames after the lease ended")

    def test_lease_cap(self):
        self.port.ready()
        self.port.expect("nack", op_id=self.port.op("hold", name="t", lease_ms=500))

    def test_heartbeat_loss_faults(self):
        self.port.ready()
        self.port.state()
        last_hb = time.monotonic()
        self.port.expect("fault", timeout=2.0)
        self.assertLess(time.monotonic() - last_hb, 1.1)

    def test_new_hello_disarms(self):
        self.port.ready()
        self.port.hello()
        self.assertEqual(self.port.state()["s"], "DISARMED")

    def test_capture_refused_when_ready(self):
        self.port.ready()
        self.port.expect("nack", op_id=self.port.op("capture", ms=500))

    def test_loop_time(self):
        self.port.hello()
        self.assertLess(self.port.state()["max_loop_us"], 100_000)

    def _next_tx(self, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            msg = self.port.read(end - time.monotonic())
            if msg and msg.get("ev") == "tx":
                return msg
        return None


if __name__ == "__main__":
    unittest.main()
