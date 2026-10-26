"""Unit tests for robotd, dogproto and robotctl: registry rules, geometry, the validator and in-process ordering."""

import asyncio
import copy
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "sim"))

import dogproto  # noqa: E402
import robotd  # noqa: E402
from devstack import DEMO_REGISTRY  # noqa: E402


def demo(**changes):
    """Return a copy of the demo registry with command field changes: name=dict."""
    doc = copy.deepcopy(DEMO_REGISTRY)
    for name, fields in changes.items():
        doc["commands"].setdefault(name, {}).update(fields)
    return doc


class RegistryRules(unittest.TestCase):
    """The load-time registry check (the microduck manifest check)."""

    def test_demo_is_valid(self):
        self.assertEqual(robotd.check_registry(DEMO_REGISTRY), [])

    def test_each_rule_refuses_its_violation(self):
        """One registry for each rule of registry.md section 3; each gives an error and none raises."""
        extra, bark_drive, list_drive, long_pulse = demo(), demo(), demo(), demo()
        extra["extra"] = 1
        bark_drive["drive"]["forward"] = "bark"
        list_drive["drive"]["forward"] = ["forward"]
        long_pulse["goto"]["forward_ms"] = 30000
        cases = {
            "unknown field": demo(bark={"colour": "red"}), "unknown top field": extra,
            "verified without payload": demo(bark={"ir": None}),
            "perpetual stop_via not verified": demo(stop={"verified": False}),
            "stop_via not a name": demo(dance={"stop_via": ["stop"]}),
            "hold over 1000 ms": demo(forward={"max_run_ms": 1500}),
            "cloud not verified": demo(bark={"verified": False}),
            "cloud episodic without natural_ms": demo(bark={"natural_ms": None}),
            "cloud episodic over 10 s": demo(bark={"natural_ms": 20000}),
            "mode command cloud": demo(program={"cloud": True}),
            "toggle protocol": demo(bark={"ir": {"proto": "RC5", "addr": 0, "cmd": 1}}),
            "unbounded run time": demo(dance={"max_run_ms": 3600000}),
            "run time not an int": demo(dance={"max_run_ms": "3000"}),
            "goto pulse over 1 s": long_pulse, "drive not a hold": bark_drive, "drive not a name": list_drive,
            "not an object": [], "command not an object": {"schema": 1, "commands": {"x": 5}},
            "drive not an object": {"schema": 1, "drive": []}, "goto not an object": {"schema": 1, "goto": None},
        }
        for rule, doc in cases.items():
            with self.subTest(rule):
                self.assertTrue(robotd.check_registry(doc))

    def test_bridge_limits(self):
        """A registry with a payload that the bridge refuses at load is never ready (protocol.md 2.4)."""
        nec = {"proto": "NEC", "addr": 0, "cmd": 1, "bits": 32, "raw_us": [], "carrier_khz": 38}
        for ir in ({**nec, "cmd": 0x100}, {**nec, "addr": 0x10000}, {**nec, "carrier_khz": 70},
                   {**nec, "carrier_khz": 38.0}, {**nec, "data": -1}, {**nec, "data": "x"}, {**nec, "raw_us": [0]},
                   {"proto": "raw", "raw_us": [60000, 60000, 30001]}, {"proto": "raw", "raw_us": [70000]},
                   {"proto": "raw", "raw_us": []}, {"proto": "raw", "raw_us": [500] * 121}, 5):
            self.assertTrue(robotd.check_registry(demo(bark={"ir": ir})), ir)
        self.assertEqual(robotd.check_registry(demo(bark={"ir": {**nec, "data": 0xE718FF00}})), [])
        self.assertEqual(robotd.check_registry(demo(bark={"ir": {"proto": "raw", "raw_us": [60000, 60000, 30000]}})), [])
        self.assertTrue(robotd.check_registry(demo(**{"\u00e9" * 16: {"ir": None}})))
        self.assertEqual(robotd.check_registry(demo(**{"\u00e9" * 15: {"ir": None}})), [])
        many = demo(**{f"c{i}": {"ir": nec} for i in range(24)})
        self.assertTrue(any("more than 24" in e for e in robotd.check_registry(many)))

    def test_unverified_drive_is_only_unavailable(self):
        """An unverified drive command is not an error; goto needs forward and one turn."""
        no_left = demo(left={"verified": False, "cloud": False})
        self.assertEqual(robotd.check_registry(no_left), [])
        self.assertFalse(robotd.capabilities(no_left)["drive"]["left"])
        self.assertTrue(robotd.capabilities(no_left)["goto"])
        no_turn = demo(left={"verified": False, "cloud": False}, right={"verified": False, "cloud": False})
        self.assertFalse(robotd.capabilities(no_turn)["goto"])
        self.assertFalse(robotd.capabilities(demo(forward={"verified": False, "cloud": False}))["goto"])


class Geometry(unittest.TestCase):
    """Stick mapping and the goto step."""

    def test_stick(self):
        """Inside the dead band there is no direction; outside it the larger axis wins."""
        self.assertIsNone(robotd.stick_dir(0.3, -0.4))
        for axes, direction in (((0.9, 0.6), "forward"), ((-0.9, 0.6), "back"), ((0.2, 0.8), "left"),
                                ((0.2, -0.8), "right")):
            self.assertEqual(robotd.stick_dir(*axes), direction)


class Params(unittest.TestCase):
    """The dogproto PARAMS validator."""

    def test_validate(self):
        """Unknown, absent and mistyped fields and unknown methods are refused; a final "?" marks an optional field."""
        for method, params, code in (
                ("robot.do", {"trick": "bark", "id": "1", "x": 1}, dogproto.INVALID_PARAMS),
                ("robot.do", {"trick": "bark"}, dogproto.INVALID_PARAMS),
                ("robot.drive", {"dir": "forward", "ms": True, "id": "1"}, dogproto.INVALID_PARAMS),
                ("robot.fly", {}, dogproto.METHOD_NOT_FOUND)):
            with self.subTest(method=method, params=params):
                with self.assertRaises(dogproto.RpcError) as ctx:
                    dogproto.validate(method, params)
                self.assertEqual(ctx.exception.code, code)
        self.assertEqual(dogproto.validate("media.snapshot", {}), {})
        self.assertEqual(dogproto.validate("media.snapshot", {"max_px": 320}), {"max_px": 320})

    def test_nan_never_reaches_the_stick(self):
        """A clamp turns NaN into full motion, so the bus refuses NaN and Infinity."""
        with self.assertRaises(ValueError):
            dogproto.decode('{"vx":NaN,"vyaw":0}')
        for params in (dogproto.decode('{"vx":1e999,"vyaw":0}'), {"vx": float("nan"), "vyaw": 0.0}):
            with self.assertRaises(dogproto.RpcError):
                dogproto.validate("robot.move", params)

    def test_cloud_methods_exclude_arming_and_stick(self):
        for method in ("robot.enable", "robot.move", "robot.capture", "robot.test", "robot.reload", "audio.ptt"):
            self.assertNotIn(method, dogproto.CLOUD_METHODS)


class StubLink:
    """A bridge link that answers ops at once, in the same loop turn as a real fast reply."""

    def __init__(self, daemon, on_op=None):
        self.daemon = daemon
        self.ops = []
        self.on_op = on_op

    def send(self, obj):
        self.ops.append(obj)
        if self.on_op is not None and self.on_op(self, obj):
            return
        if "id" in obj:
            loop = asyncio.get_running_loop()
            loop.call_soon(self.daemon._on_esp, {"ev": "ack", "id": obj["id"]})
            if obj["op"] == "send":
                loop.call_soon(self.daemon._on_esp, {"ev": "tx", "id": obj["id"], "frames": 1})
                loop.call_soon(self.daemon._on_esp, {"ev": "done", "id": obj["id"]})

    def close(self):
        pass

    def names(self):
        """Return ops as short strings such as 'send dance' or 'stop'."""
        return [f"{o['op']} {o['name']}" if "name" in o else o["op"] for o in self.ops]


class Conn:
    """A stand-in for dogproto.Conn."""

    def __init__(self, cloud):
        self.cloud = cloud


class InProcess(unittest.IsolatedAsyncioTestCase):
    """robotd handlers with a stub link: ordering and race rules that need exact control."""

    def make(self, registry=None, on_op=None):
        path = os.path.join(tempfile.mkdtemp(), "registry.json")
        with open(path, "w") as handle:
            json.dump(DEMO_REGISTRY if registry is None else registry, handle)
        daemon = robotd.Robotd("/nonexistent", path)
        daemon.link = StubLink(daemon, on_op)
        daemon.link_up = True
        daemon.armed = True
        daemon._esp_rx = time.monotonic() + 3600  # keep the silence watchdog quiet for direct tick() calls
        return daemon

    async def test_stop_and_disarm_latch_a_held_stick(self):
        """A stop or a disarm must hold while the stick stays pushed; padd repeats the stick at 20 Hz."""
        for stop in ("h_stop", "h_disarm"):
            with self.subTest(stop):
                daemon = self.make()
                local = Conn(False)
                await daemon.h_move({"vx": 1.0, "vyaw": 0.0}, local)
                daemon.tick(1)
                self.assertIn("hold forward", daemon.link.names())
                await getattr(daemon, stop)({}, local)
                daemon.armed = True  # Start pressed again with the stick still pushed
                stopped_at = len(daemon.link.ops)
                for tick in range(2, 10):
                    await daemon.h_move({"vx": 1.0, "vyaw": 0.0}, local)
                    daemon.tick(tick)
                self.assertNotIn("hold forward", daemon.link.names()[stopped_at:])
                await daemon.h_move({"vx": 0.0, "vyaw": 0.0}, local)  # stick back at center
                await daemon.h_move({"vx": 1.0, "vyaw": 0.0}, local)
                daemon.tick(11)
                self.assertIn("hold forward", daemon.link.names()[stopped_at:])

    async def test_duplicate_hello_reply_keeps_the_link(self):
        """A repeated hello can get two replies with our sid; only another sid means a board reset."""
        daemon = self.make()
        daemon._sid = 1234
        daemon._on_esp({"ev": "hello", "sid": 1234, "boot": 1, "fw": "x", "state": "DISARMED"})
        self.assertTrue(daemon.link_up)
        self.assertTrue(daemon.armed)
        daemon._on_esp({"ev": "hello", "sid": 0, "boot": 2, "fw": "x", "state": "DISARMED"})
        self.assertFalse(daemon.link_up)
        self.assertFalse(daemon.armed)

    async def test_arm_races_leave_the_dog_disarmed(self):
        """A bridge reset or a disarm that lands with the arm ack must not leave robotd armed."""
        for race in ("reset", "disarm"):
            def ack_then_race(link, obj):
                if obj["op"] != "arm":
                    return False
                def both():
                    link.daemon._on_esp({"ev": "ack", "id": obj["id"]})
                    if race == "reset":
                        link.daemon._on_esp({"ev": "hello", "sid": 0, "boot": 2, "fw": "x", "state": "DISARMED"})
                    else:
                        link.daemon._disarm("request")
                asyncio.get_running_loop().call_soon(both)
                return True

            with self.subTest(race):
                daemon = self.make(on_op=ack_then_race)
                daemon.armed = False
                daemon._sid = 1234
                with self.assertRaises(dogproto.RpcError):
                    await daemon.h_enable({}, Conn(False))
                self.assertFalse(daemon.armed)

    async def test_one_capture_at_a_time(self):
        """A second capture must not take the frames of the first one."""
        daemon = self.make()
        daemon.armed = False
        first = asyncio.ensure_future(daemon.h_capture({"ms": 500}, Conn(False)))
        await asyncio.sleep(0.01)
        with self.assertRaises(dogproto.RpcError) as ctx:
            await daemon.h_capture({"ms": 500}, Conn(False))
        self.assertEqual(ctx.exception.code, dogproto.BUSY)
        daemon._on_esp({"ev": "cap", "proto": "NEC"})
        daemon._on_esp({"ev": "done", "id": daemon.link.ops[-1]["id"]})
        self.assertEqual((await first)["frames"], [{"proto": "NEC"}])


if __name__ == "__main__":
    unittest.main()
