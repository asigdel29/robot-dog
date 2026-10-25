"""Unit tests for robotd, dogproto and robotctl: registry rules, geometry, the validator and in-process ordering."""

import copy
import os
import sys
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


if __name__ == "__main__":
    unittest.main()
