"""Unit tests for the pure parts of robotd, dogproto and robotctl: rules, mappings, geometry."""

import copy
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "sim"))

import dogproto  # noqa: E402
import robotctl  # noqa: E402
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

    def test_unknown_field_refused(self):
        self.assertTrue(robotd.check_registry(demo(bark={"colour": "red"})))
        doc = demo()
        doc["extra"] = 1
        self.assertTrue(robotd.check_registry(doc))

    def test_verified_needs_payload(self):
        self.assertTrue(robotd.check_registry(demo(bark={"ir": None})))

    def test_hold_cap(self):
        self.assertTrue(robotd.check_registry(demo(forward={"max_run_ms": 1500})))

    def test_perpetual_needs_verified_stop(self):
        self.assertTrue(robotd.check_registry(demo(stop={"verified": False})))

    def test_cloud_rules(self):
        self.assertTrue(robotd.check_registry(demo(bark={"verified": False})))
        self.assertTrue(robotd.check_registry(demo(program={"cloud": True})))
        self.assertTrue(robotd.check_registry(demo(bark={"natural_ms": None})))
        self.assertTrue(robotd.check_registry(demo(bark={"natural_ms": 20000})))

    def test_toggle_protocol_refused(self):
        self.assertTrue(robotd.check_registry(demo(bark={"ir": {"proto": "RC5", "addr": 0, "cmd": 1}})))

    def test_drive_must_be_hold(self):
        doc = demo()
        doc["drive"]["forward"] = "bark"
        self.assertTrue(robotd.check_registry(doc))

    def test_unverified_drive_is_not_an_error(self):
        doc = demo(left={"verified": False, "cloud": False}, right={"verified": False, "cloud": False})
        self.assertEqual(robotd.check_registry(doc), [])
        caps = robotd.capabilities(doc)
        self.assertFalse(caps["drive"]["left"])
        self.assertFalse(caps["goto"])

    def test_goto_needs_forward_and_a_turn(self):
        self.assertTrue(robotd.capabilities(DEMO_REGISTRY)["goto"])
        self.assertTrue(robotd.capabilities(demo(left={"verified": False, "cloud": False}))["goto"])
        self.assertFalse(robotd.capabilities(demo(forward={"verified": False, "cloud": False}))["goto"])


class Geometry(unittest.TestCase):
    """Stick mapping and the goto step."""

    def test_stick_dead_band(self):
        self.assertIsNone(robotd.stick_dir(0.3, -0.4))

    def test_stick_larger_axis_wins(self):
        self.assertEqual(robotd.stick_dir(0.9, 0.6), "forward")
        self.assertEqual(robotd.stick_dir(-0.9, 0.6), "back")
        self.assertEqual(robotd.stick_dir(0.2, 0.8), "left")
        self.assertEqual(robotd.stick_dir(0.2, -0.8), "right")

    def test_nav_done_inside_radius(self):
        self.assertEqual(robotd.nav_step((100, 100, 0), (120, 100), 40), "done")

    def test_nav_forward_when_aligned(self):
        self.assertEqual(robotd.nav_step((100, 100, 0), (300, 110), 40), "forward")

    def test_nav_turns_toward_goal(self):
        # Goal straight below on screen (y down): clockwise, so right.
        self.assertEqual(robotd.nav_step((100, 100, 0), (100, 300), 40), "right")
        self.assertEqual(robotd.nav_step((100, 100, 0), (100, -100), 40), "left")
        self.assertEqual(robotd.nav_step((100, 100, 0), (100, 300), 40, turn_sign=-1), "left")

    def test_inside_polygon(self):
        square = [[0, 0], [10, 0], [10, 10], [0, 10]]
        self.assertTrue(robotd.inside((5, 5), square))
        self.assertFalse(robotd.inside((15, 5), square))

    def test_wrap(self):
        self.assertAlmostEqual(robotd.wrap(3 * math.pi), math.pi)


class Params(unittest.TestCase):
    """The dogproto PARAMS validator."""

    def test_unknown_and_missing_fields(self):
        with self.assertRaises(dogproto.RpcError) as ctx:
            dogproto.validate("robot.do", {"trick": "bark", "id": "1", "x": 1})
        self.assertEqual(ctx.exception.code, dogproto.INVALID_PARAMS)
        with self.assertRaises(dogproto.RpcError):
            dogproto.validate("robot.do", {"trick": "bark"})

    def test_bool_is_not_int(self):
        with self.assertRaises(dogproto.RpcError):
            dogproto.validate("robot.drive", {"dir": "forward", "ms": True, "id": "1"})

    def test_optional_field(self):
        self.assertEqual(dogproto.validate("media.snapshot", {}), {})
        self.assertEqual(dogproto.validate("media.snapshot", {"max_px": 320}), {"max_px": 320})

    def test_unknown_method(self):
        with self.assertRaises(dogproto.RpcError) as ctx:
            dogproto.validate("robot.fly", {})
        self.assertEqual(ctx.exception.code, dogproto.METHOD_NOT_FOUND)

    def test_cloud_methods_exclude_arming_and_stick(self):
        for method in ("robot.enable", "robot.move", "robot.capture", "robot.test", "robot.reload", "audio.ptt"):
            self.assertNotIn(method, dogproto.CLOUD_METHODS)


class Capture(unittest.TestCase):
    """robotctl builds a registry entry from captures."""

    TAP = {"proto": "NEC", "addr": 0, "cmd": 0x18, "bits": 32, "raw": [9000, 4500, 560], "gap_us": 0}
    REPEAT = {"proto": "NEC", "addr": 0, "cmd": 0x18, "bits": 0, "raw": [9000, 2250, 560], "gap_us": 108000,
              "repeat": True}

    def test_nec_hold(self):
        entry = robotctl.entry_from_captures([self.TAP] * 3, [self.TAP, self.REPEAT, self.REPEAT])
        self.assertEqual((entry["kind"], entry["repeat"], entry["period_ms"]), ("hold", "nec", 108))
        self.assertFalse(entry["verified"])

    def test_single_press(self):
        entry = robotctl.entry_from_captures([self.TAP] * 3, [self.TAP])
        self.assertEqual((entry["kind"], entry["repeat"]), ("episodic", "none"))

    def test_mismatch_refused(self):
        with self.assertRaises(robotctl.InputError):
            robotctl.entry_from_captures([self.TAP, {**self.TAP, "cmd": 1}, self.TAP], [self.TAP])

    def test_toggle_refused(self):
        with self.assertRaises(robotctl.InputError):
            robotctl.entry_from_captures([{**self.TAP, "proto": "RC5"}] * 3, [])

    def test_raw_timings_compared(self):
        raw = {"proto": "raw", "raw": [1000, 500, 1000, 500], "gap_us": 0}
        entry = robotctl.entry_from_captures([raw, {**raw, "raw": [1050, 480, 990, 520]}, raw], [raw])
        self.assertEqual(entry["ir"]["proto"], "raw")
        with self.assertRaises(robotctl.InputError):
            robotctl.entry_from_captures([raw, {**raw, "raw": [2000, 500, 1000, 500]}, raw], [raw])


if __name__ == "__main__":
    unittest.main()
