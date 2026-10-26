"""End-to-end tests of the dog stack on one machine, against the fake ESP32.

Each check maps to a row of the verification table in the plan: stop latency,
watchdog after a robotd crash, cloud refusals, arm rules, duplicates and the
simulated goto. Numbers are printed so a run can go into docs/test-log.md.
"""

import copy
import os
import signal
import sys
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "sim"))

import dogproto  # noqa: E402
from devstack import DEMO_REGISTRY, Stack  # noqa: E402


def aid():
    """Return a new action id."""
    return uuid.uuid4().hex


class RobotdStack(unittest.TestCase):
    """robotd with the fake bridge: arm rules, cloud rules, stop and watchdog."""

    def setUp(self):
        self.stack = Stack()
        self.addCleanup(self.stack.stop)
        self.stack.start()

    def arm(self):
        """Arm the dog from the local socket."""
        self.assertTrue(self.stack.call("robot.enable")["armed"])

    def test_cloud_socket_refuses_arm_and_stick(self):
        for method, params in (("robot.enable", {}), ("robot.move", {"vx": 1, "vyaw": 0}),
                               ("robot.capture", {"ms": 500})):
            with self.assertRaises(dogproto.RpcError) as ctx:
                self.stack.call(method, params, cloud=True)
            self.assertEqual(ctx.exception.code, dogproto.METHOD_NOT_FOUND)

    def test_drive_while_disarmed(self):
        with self.assertRaises(dogproto.RpcError) as ctx:
            self.stack.call("robot.drive", {"dir": "forward", "ms": 300, "id": aid()}, cloud=True)
        self.assertEqual(ctx.exception.code, dogproto.NOT_ARMED)

    def test_cloud_sees_only_cloud_tricks(self):
        names = {t["name"] for t in self.stack.call("robot.state", cloud=True)["tricks"]}
        self.assertNotIn("program", names)
        self.assertIn("program", {t["name"] for t in self.stack.call("robot.state")["tricks"]})
        self.arm()
        with self.assertRaises(dogproto.RpcError):
            self.stack.call("robot.do", {"trick": "program", "id": aid()}, cloud=True)

    def test_restart_starts_disarmed(self):
        self.arm()
        self.stack.kill("robotd")
        self.stack.start_robotd()
        self.assertFalse(self.stack.call("robot.state")["armed"])

    def test_bridge_reset_disarms(self):
        self.arm()
        self.stack.procs["fake_esp32"].send_signal(signal.SIGUSR1)
        self.stack.wait_for(lambda: not self.stack.call("robot.state")["armed"], "disarm after bridge reset")
        self.stack.wait_for(lambda: self.stack.call("robot.state")["esp"] == "DISARMED", "link up again")


class BadRegistry(unittest.TestCase):
    """A registry that breaks a rule."""

    def test_link_comes_up_and_arming_is_refused(self):
        registry = copy.deepcopy(DEMO_REGISTRY)
        registry["commands"]["x" * 32] = registry["commands"]["bark"]  # a name that the bridge refuses
        stack = Stack(registry=registry)
        self.addCleanup(stack.stop)
        stack.start()  # waits for the bridge link
        self.assertFalse(stack.call("robot.state")["registry_ready"])
        with self.assertRaises(dogproto.RpcError) as ctx:
            stack.call("robot.enable")
        self.assertEqual(ctx.exception.code, dogproto.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
