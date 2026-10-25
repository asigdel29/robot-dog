"""Unit tests for robotd, dogproto and robotctl: rules, mappings, geometry, and in-process ordering checks."""

import asyncio
import copy
import json
import math
import os
import sys
import tempfile
import time
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
        errors = robotd.check_registry(demo(bark={"ir": {"proto": "RC5", "addr": 0, "cmd": 1}}))
        self.assertTrue(any("protocol RC5 refused" in e for e in errors), errors)

    def test_run_times_are_bounded(self):
        self.assertTrue(robotd.check_registry(demo(dance={"max_run_ms": 3600000})))
        self.assertTrue(robotd.check_registry(demo(dance={"max_run_ms": "3000"})))
        doc = demo()
        doc["goto"]["forward_ms"] = 30000
        self.assertTrue(robotd.check_registry(doc))

    def test_wrong_types_do_not_crash(self):
        for doc in ({"schema": 1, "commands": {"x": 5}}, {"schema": 1, "drive": []}, {"schema": 1, "goto": None}):
            self.assertTrue(robotd.check_registry(doc))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"schema": 1, "commands": {"x": 5}}, handle)
        registry, errors = robotd.load_registry(handle.name)
        self.assertIsNone(registry)
        self.assertTrue(errors)

    def test_cloud_capabilities_need_cloud_flag(self):
        doc = demo(forward={"cloud": False})
        self.assertTrue(robotd.capabilities(doc)["drive"]["forward"])
        self.assertFalse(robotd.capabilities(doc, cloud=True)["drive"]["forward"])
        self.assertFalse(robotd.capabilities(doc, cloud=True)["goto"])

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
        for proto in ("RC5", "RC6", "RC6A", "Marantz"):
            with self.assertRaises(robotctl.InputError):
                robotctl.entry_from_captures([{**self.TAP, "proto": proto}] * 3, [])

    def test_raw_timings_compared(self):
        raw = {"proto": "raw", "raw": [1000, 500, 1000, 500], "gap_us": 0}
        entry = robotctl.entry_from_captures([raw, {**raw, "raw": [1050, 480, 990, 520]}, raw], [raw])
        self.assertEqual(entry["ir"]["proto"], "raw")
        with self.assertRaises(robotctl.InputError):
            robotctl.entry_from_captures([raw, {**raw, "raw": [2000, 500, 1000, 500]}, raw], [raw])


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

    async def test_stop_in_same_turn_as_ack_is_not_lost(self):
        """A stop that lands with the first hold ack must end the drive (Python 3.11 wait_for drops it)."""
        def ack_then_stop(link, obj):
            if obj["op"] == "hold" and link.names().count("hold forward") == 1:
                def both():
                    link.daemon._on_esp({"ev": "ack", "id": obj["id"]})
                    link.daemon._esp_quiet("stop")
                    link.daemon._cancel_action("stopped")
                asyncio.get_running_loop().call_soon(both)
                return True
            return False

        daemon = self.make(on_op=ack_then_stop)
        with self.assertRaises(dogproto.RpcError) as ctx:
            await daemon.h_drive({"dir": "forward", "ms": 600, "id": "a"}, Conn(True))
        self.assertEqual(ctx.exception.code, dogproto.PREEMPTED)
        names = daemon.link.names()
        self.assertEqual(names[names.index("stop"):].count("hold forward"), 0, names)

    async def test_disarm_during_perpetual_sends_stop_command_first(self):
        daemon = self.make()
        task = asyncio.ensure_future(daemon.h_do({"trick": "dance", "id": "d"}, Conn(True)))
        await asyncio.sleep(0.05)
        daemon._disarm("request")
        with self.assertRaises(dogproto.RpcError):
            await task
        names = daemon.link.names()
        self.assertEqual(names[names.index("send dance"):], ["send dance", "stop", "send stop", "disarm"])

    async def test_stick_preempt_of_perpetual_sends_stop_command_before_hold(self):
        daemon = self.make()
        task = asyncio.ensure_future(daemon.h_do({"trick": "dance", "id": "d"}, Conn(True)))
        await asyncio.sleep(0.05)
        daemon._drive_stick("left")
        with self.assertRaises(dogproto.RpcError):
            await task
        daemon._drive_stick("left")
        names = daemon.link.names()
        self.assertEqual(names[names.index("send dance"):], ["send dance", "send stop", "hold left"])

    async def test_stop_latches_a_held_stick(self):
        """A stop must hold while the stick stays pushed; padd re-sends the stick at 20 Hz."""
        daemon = self.make()
        local = Conn(False)
        await daemon.h_move({"vx": 1.0, "vyaw": 0.0}, local)
        daemon.tick(1)
        self.assertIn("hold forward", daemon.link.names())
        await daemon.h_stop({}, local)
        stopped_at = len(daemon.link.ops)
        for tick in range(2, 10):
            await daemon.h_move({"vx": 1.0, "vyaw": 0.0}, local)
            daemon.tick(tick)
        self.assertNotIn("hold forward", daemon.link.names()[stopped_at:])
        await daemon.h_move({"vx": 0.0, "vyaw": 0.0}, local)  # stick back at center
        await daemon.h_move({"vx": 1.0, "vyaw": 0.0}, local)
        daemon.tick(11)
        self.assertIn("hold forward", daemon.link.names()[stopped_at:])

    async def test_disarm_latches_a_held_stick(self):
        daemon = self.make()
        local = Conn(False)
        await daemon.h_move({"vx": 1.0, "vyaw": 0.0}, local)
        daemon._disarm("request")
        daemon.armed = True  # Start pressed again with the stick still pushed
        disarmed_at = len(daemon.link.ops)
        await daemon.h_move({"vx": 1.0, "vyaw": 0.0}, local)
        daemon.tick(1)
        self.assertNotIn("hold forward", daemon.link.names()[disarmed_at:])

    async def test_cloud_drive_needs_cloud_flag(self):
        daemon = self.make(registry=demo(forward={"cloud": False}))
        with self.assertRaises(dogproto.RpcError) as ctx:
            await daemon.h_drive({"dir": "forward", "ms": 300, "id": "a"}, Conn(True))
        self.assertEqual(ctx.exception.code, dogproto.UNAVAILABLE)
        self.assertFalse(daemon.state(Conn(True))["caps"]["drive"]["forward"])
        await daemon.h_drive({"dir": "forward", "ms": 100, "id": "b"}, Conn(False))

    async def test_bad_marker_is_refused_and_does_not_wedge(self):
        daemon = self.make()
        with self.assertRaises(dogproto.RpcError):
            await daemon.h_observe({"t_mono_ns": 1, "markers": {"1": 5}}, Conn(False))
        result = await daemon.h_do({"trick": "bark", "id": "b"}, Conn(True))
        self.assertTrue(result["transmitted"])

    async def test_nan_never_reaches_the_stick(self):
        with self.assertRaises(ValueError):
            dogproto.decode('{"jsonrpc":"2.0","method":"robot.move","params":{"vx":NaN,"vyaw":0}}')
        with self.assertRaises(dogproto.RpcError):
            dogproto.validate("robot.move", {"vx": float("nan"), "vyaw": 0.0})
        daemon = self.make()
        with self.assertRaises(dogproto.RpcError):
            await daemon.h_move({"vx": float("inf"), "vyaw": 0.0}, Conn(False))

    async def test_failed_action_id_is_not_run_again(self):
        daemon = self.make()
        daemon._observed = lambda before: 1 / 0
        for _ in range(2):
            with self.assertRaises(dogproto.RpcError) as ctx:
                await daemon.h_do({"trick": "bark", "id": "same"}, Conn(True))
            self.assertEqual(ctx.exception.code, dogproto.INTERNAL_ERROR)
        self.assertEqual(daemon.link.names().count("send bark"), 1)


if __name__ == "__main__":
    unittest.main()
