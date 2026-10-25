"""Serial protocol conformance for the IR bridge.

The same tests run against sim/fake_esp32.py (default) and against the real
Feather when DOG_SERIAL names its port, which keeps the fake honest. The board
needs no IR target: the tests load a dummy NEC entry.
"""

import itertools
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
ENTRY = {"name": "t", "proto": "NEC", "addr": 0, "cmd": 1, "raw": [], "khz": 38, "repeat": "nec", "period_ms": 108}
_fake = None
_port = os.environ.get("DOG_SERIAL")
#: Timing slack: tight for the real board, wider for the fake, which shares a busy host.
SLACK_S = 0.15 if _port else 0.4


def setUpModule():
    """Start the fake bridge unless DOG_SERIAL names a real board."""
    global _fake, _port
    if _port:
        return
    run_dir = tempfile.mkdtemp(prefix="dog-conf-")
    _fake = subprocess.Popen([sys.executable, os.path.join(ROOT, "sim", "fake_esp32.py"), "--run-dir", run_dir],
                             stdout=subprocess.PIPE, text=True)
    _port = _fake.stdout.readline().split()[1]


def tearDownModule():
    """Stop the fake bridge."""
    if _fake is not None:
        _fake.terminate()
        _fake.wait(5)


class Port:
    """Raw access to the bridge, as robotd would see it."""

    def __init__(self):
        self.fd = os.open(_port, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIFLUSH)
        self.buf = b""
        self.ids = itertools.count(1)

    def close(self):
        """Close the port."""
        os.close(self.fd)

    def send(self, **msg):
        """Write one line."""
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
                    self.ids = itertools.count(1)
                    return msg
        raise AssertionError("no hello reply")

    def ready(self):
        """hello, load one entry and arm; return when READY."""
        self.hello()
        self.expect("ack", op_id=self.op("load", **ENTRY))
        self.expect("ack", op_id=self.op("arm", n=1))

    def state(self):
        """Send a heartbeat and return the state event."""
        self.send(op="hb", seq=1)
        return self.expect("state")


class Conformance(unittest.TestCase):
    """Firmware rules from docs/design/protocol.md."""

    def setUp(self):
        self.port = Port()

    def tearDown(self):
        self.port.send(op="disarm", id=999_999)
        self.port.close()

    def test_new_hello_disarms(self):
        self.port.ready()
        self.assertEqual(self.port.hello()["state"], "DISARMED")
        self.assertEqual(self.port.state()["s"], "DISARMED")

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
        self.assertEqual([m for m in self._events(0.3) if m.get("ev") == "tx"], [], "repeat frames after the lease")

    def test_lease_cap(self):
        self.port.ready()
        self.port.expect("nack", op_id=self.port.op("hold", name="t", lease_ms=500))

    def test_heartbeat_loss_faults(self):
        self.port.ready()
        self.port.state()
        last_hb = time.monotonic()
        self.port.expect("fault", timeout=2.0)
        self.assertLess(time.monotonic() - last_hb, 1.1 if _fake is None else 1.0 + SLACK_S)

    def test_capture_refused_when_ready(self):
        self.port.ready()
        self.port.expect("nack", op_id=self.port.op("capture", ms=500))

    def test_loop_time(self):
        self.port.hello()
        self.assertLess(self.port.state()["max_loop_us"], 100_000)

    def test_limits_and_types(self):
        """Each field outside the limits of protocol.md 2.4, absent or of the wrong JSON type is refused."""
        self.port.hello()
        self.port.expect("ack", op_id=self.port.op("load", **{**ENTRY, "name": "d", "data": 0xE718FF00}))
        missing = {k: v for k, v in ENTRY.items() if k != "khz"}
        self.assertEqual(self.port.expect("nack", op_id=self.port.op("load", **missing))["why"], "bad entry")
        for bad in ({"proto": "RC5"}, {"repeat": "toggle"}, {"proto": "raw", "raw": []}, {"raw": [560] * 121},
                    {"proto": "raw", "raw": [60000, 60000, 30001]}, {"name": "\u00e9" * 16}, {"cmd": 0x100},
                    {"khz": None}, {"khz": "38"}, {"khz": 38.5}, {"addr": -1}, {"period_ms": True}, {"raw": "x"},
                    {"raw": [560.5]}, {"name": 5}, {"data": "x"}, {"data": -1}):
            self.assertEqual(self.port.expect("nack", op_id=self.port.op("load", **{**ENTRY, **bad}))["why"], "bad entry")
        self.assertEqual(self.port.expect("nack", op_id=self.port.op("capture", ms="x"))["why"], "bad ms")
        for i in range(23):
            self.port.expect("ack", op_id=self.port.op("load", **{**ENTRY, "name": f"e{i}"}))
        self.assertEqual(self.port.expect("nack", op_id=self.port.op("load", **ENTRY))["why"], "table full")
        self.port.expect("nack", op_id=self.port.op("arm", n=True))
        self.port.expect("ack", op_id=self.port.op("arm", n=24))
        self.assertEqual(self.port.expect("nack", op_id=self.port.op("hold", name="d", lease_ms=300.0))["why"], "lease")
        self.assertEqual(self.port.expect("nack", op_id=self.port.op("send", name=["d"]))["why"], "unknown name")

    def test_hold_without_repeat_sends_one_frame(self):
        self.port.hello()
        self.port.expect("ack", op_id=self.port.op("load", **{**ENTRY, "repeat": "none"}))
        self.port.expect("ack", op_id=self.port.op("arm", n=1))
        self.assertEqual(self._frames_until_done(self.port.op("hold", name="t", lease_ms=300)), 1)

    def test_short_lease_sends_one_frame(self):
        self.port.ready()
        self.assertEqual(self._frames_until_done(self.port.op("hold", name="t", lease_ms=5)), 1)

    def test_heartbeat_loss_sends_stop_once(self):
        self.port.hello()
        for name in ("t", "stop"):
            self.port.expect("ack", op_id=self.port.op("load", **{**ENTRY, "name": name}))
        self.port.expect("ack", op_id=self.port.op("arm", n=2))
        self.port.state()
        events = self._events(2.0, lambda m: m.get("ev") == "fault")
        self.assertEqual([m for m in events if m.get("ev") == "tx"], [{"ev": "tx", "id": 0, "frames": 1}])

    def test_send_ends_before_the_next_op(self):
        self.port.ready()
        send_id = self.port.op("send", name="t")
        disarm_id = self.port.op("disarm")
        events = self._events(1.0, lambda m: m.get("ev") == "ack" and m.get("id") == disarm_id)
        order = [(m["ev"], m["id"]) for m in events if m.get("id") in (send_id, disarm_id)]
        self.assertEqual(order, [("ack", send_id), ("tx", send_id), ("done", send_id), ("ack", disarm_id)])

    def test_gap_before_a_new_command(self):
        self.port.ready()
        first = self.port.op("send", name="t")
        second = self.port.op("send", name="t")
        self.port.expect("done", op_id=first)
        done_at = time.monotonic()
        self.port.expect("tx", op_id=second)
        # The second frame starts 40 ms after the first ends and lasts about 70 ms; with no gap it would be 70 ms.
        self.assertGreater(time.monotonic() - done_at, 0.09)

    def test_arm_refused_while_capturing(self):
        self.port.hello()
        self.port.expect("ack", op_id=self.port.op("load", **ENTRY))
        cap_id = self.port.op("capture", ms=300)
        self.port.expect("ack", op_id=cap_id)
        self.assertEqual(self.port.expect("nack", op_id=self.port.op("arm", n=1))["why"], "capturing")
        self.port.expect("done", op_id=cap_id)
        self.port.expect("ack", op_id=self.port.op("arm", n=1))

    def test_send_after_stop(self):
        self.port.ready()
        self.port.expect("ack", op_id=self.port.op("hold", name="t", lease_ms=300))
        self.port.expect("ack", op_id=self.port.op("stop"))
        op_id = self.port.op("send", name="t")
        self.port.expect("ack", op_id=op_id)
        self.port.expect("done", op_id=op_id)

    def _events(self, timeout, last=None):
        """Return the events up to and with the first one for which last() is true (fail after timeout).

        Without last, return each event within timeout.
        """
        events = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            msg = self.port.read(end - time.monotonic())
            if msg:
                events.append(msg)
                if last and last(msg):
                    return events
        if last:
            self.fail(f"no closing event within {timeout} s: {events}")
        return events

    def _frames_until_done(self, op_id):
        """Return the tx count until the done of op_id."""
        events = self._events(1.0, lambda m: m.get("ev") == "done" and m.get("id") == op_id)
        return sum(m.get("ev") == "tx" for m in events)


if __name__ == "__main__":
    unittest.main()
