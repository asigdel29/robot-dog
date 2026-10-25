"""End-to-end tests of the dog stack on one machine, against the fake ESP32.

Each check maps to a row of the verification table in the plan: stop latency,
watchdog after a robotd crash, cloud refusals, arm rules, duplicates and the
simulated goto. Latency numbers are printed so a run can go into docs/test-log.md.
"""

import asyncio
import contextlib
import json
import math
import os
import random
import statistics
import sys
import threading
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "sim"))

import dogproto  # noqa: E402
from devstack import Stack  # noqa: E402


def aid():
    """Return a new action id."""
    return uuid.uuid4().hex


class RobotdStack(unittest.TestCase):
    """robotd with the fake bridge: arm rules, cloud rules, stop and watchdog."""

    def setUp(self):
        self.stack = Stack()
        self.stack.start()

    def tearDown(self):
        self.stack.stop()

    def arm(self):
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

    def test_cloud_trick_reaches_bridge(self):
        self.arm()
        result = self.stack.call("robot.do", {"trick": "bark", "id": aid()}, cloud=True, timeout=5)
        self.assertTrue(result["transmitted"])
        self.assertEqual([m["name"] for _, m in self.stack.wait_ops("send", 1)], ["bark"])

    def test_cloud_sees_only_cloud_tricks(self):
        names = {t["name"] for t in self.stack.call("robot.state", cloud=True)["tricks"]}
        self.assertNotIn("program", names)
        self.assertIn("program", {t["name"] for t in self.stack.call("robot.state")["tricks"]})
        self.arm()
        with self.assertRaises(dogproto.RpcError):
            self.stack.call("robot.do", {"trick": "program", "id": aid()}, cloud=True)

    def test_duplicate_id_moves_once(self):
        self.arm()
        action = aid()
        first = self.stack.call("robot.do", {"trick": "bark", "id": action}, cloud=True)
        second = self.stack.call("robot.do", {"trick": "bark", "id": action}, cloud=True)
        self.assertEqual(first, second)
        self.assertEqual(len(self.stack.wait_ops("send", 2, timeout=0.3)), 1)

    def test_over_cap_refused(self):
        self.arm()
        with self.assertRaises(dogproto.RpcError) as ctx:
            self.stack.call("robot.drive", {"dir": "forward", "ms": 5000, "id": aid()}, cloud=True)
        self.assertEqual(ctx.exception.code, dogproto.INVALID_PARAMS)

    def test_perpetual_ends_with_stop_command(self):
        self.arm()
        self.stack.call("robot.do", {"trick": "dance", "id": aid()}, cloud=True, timeout=10)
        self.assertEqual([m["name"] for _, m in self.stack.wait_ops("send", 2)], ["dance", "stop"])

    def test_stop_latency(self):
        """p99 from a robot.stop request to the stop op at the bridge, over 200 trials."""
        self.arm()
        samples = []

        async def trials():
            client = await dogproto.Client.connect(self.stack.sock("robotd", cloud=True))
            local = await dogproto.Client.connect(self.stack.sock("robotd"))
            try:
                for _ in range(200):
                    drive = asyncio.ensure_future(
                        client.call("robot.drive", {"dir": "forward", "ms": 800, "id": aid()}))
                    await asyncio.sleep(0.03)
                    before = time.monotonic_ns()
                    await local.call("robot.stop")
                    with self.assertRaises(dogproto.RpcError) as ctx:
                        await drive
                    self.assertEqual(ctx.exception.code, dogproto.PREEMPTED)
                    stops = self.stack.wait_ops("stop", 1, since_ns=before)
                    samples.append((stops[0][0] - before) / 1e6)
            finally:
                await client.close()
                await local.close()

        asyncio.run(trials())
        p99 = statistics.quantiles(samples, n=100)[98]
        print(f"\nstop latency ms: p50={statistics.median(samples):.2f} p99={p99:.2f} max={max(samples):.2f}")
        self.assertLessEqual(p99, 75.0)

    def test_robotd_crash_mid_hold(self):
        """kill -9 robotd during a hold: repeats stop within the lease, FAULT within 1.1 s."""
        self.arm()

        async def kill_during_drive():
            # Keep the client open: a closed owner connection would make robotd stop the hold itself.
            client = await dogproto.Client.connect(self.stack.sock("robotd", cloud=True))
            drive = asyncio.ensure_future(client.call("robot.drive", {"dir": "forward", "ms": 1000, "id": aid()}))
            await asyncio.sleep(0.2)
            killed_ns = time.monotonic_ns()
            self.stack.kill("robotd")
            with contextlib.suppress(Exception):
                await drive
            await client.close()
            return killed_ns

        killed = asyncio.run(kill_during_drive())
        self.assertEqual(self.stack.ops("stop", since_ns=killed - 250_000_000), [], "robotd stopped the hold first")
        time.sleep(1.5)
        out = [(t, m) for t, d, m in self.stack.log() if d == "out"]
        last_tx = max(t for t, m in out if m.get("ev") == "tx" and m.get("id"))
        fault = min(t for t, m in out if m.get("ev") == "fault" and t > killed)
        print(f"\nafter kill: last repeat {(last_tx - killed) / 1e6:.0f} ms, fault {(fault - killed) / 1e6:.0f} ms")
        self.assertGreater(last_tx, killed, "no repeat frame after the kill: the hold was not running")
        self.assertLessEqual((last_tx - killed) / 1e6, 400)
        self.assertLessEqual((fault - killed) / 1e6, 1100)

    def test_owner_disconnect_stops_hold(self):
        self.arm()

        async def close_during_drive():
            client = await dogproto.Client.connect(self.stack.sock("robotd", cloud=True))
            drive = asyncio.ensure_future(client.call("robot.drive", {"dir": "forward", "ms": 1000, "id": aid()}))
            await asyncio.sleep(0.2)
            closed_ns = time.monotonic_ns()
            await client.close()
            with contextlib.suppress(Exception):
                await drive
            return closed_ns

        closed = asyncio.run(close_during_drive())
        stops = self.stack.wait_ops("stop", 1, since_ns=closed)
        self.assertTrue(stops, "robotd kept the hold after its owner closed")
        print(f"\nowner close to stop: {(stops[0][0] - closed) / 1e6:.1f} ms")
        self.assertLess((stops[0][0] - closed) / 1e6, 100)

    def test_restart_starts_disarmed(self):
        self.arm()
        self.stack.kill("robotd")
        self.stack.start_robotd()
        self.assertFalse(self.stack.call("robot.state")["armed"])

    def test_bridge_reset_disarms(self):
        self.arm()
        self.stack.procs["fake_esp32"].send_signal(__import__("signal").SIGUSR1)
        self.stack.wait_for(lambda: not self.stack.call("robot.state")["armed"], "disarm after bridge reset")
        self.stack.wait_for(lambda: self.stack.call("robot.state")["esp"] == "DISARMED", "link up again")

    def test_stick_preempts_cloud_action(self):
        self.arm()

        async def scenario():
            cloud = await dogproto.Client.connect(self.stack.sock("robotd", cloud=True))
            local = await dogproto.Client.connect(self.stack.sock("robotd"))
            try:
                drive = asyncio.ensure_future(cloud.call("robot.drive", {"dir": "forward", "ms": 900, "id": aid()}))
                await asyncio.sleep(0.1)
                local.notify("robot.move", {"vx": 0.0, "vyaw": 1.0})
                with self.assertRaises(dogproto.RpcError) as ctx:
                    await drive
                return ctx.exception.code
            finally:
                await cloud.close()
                await local.close()

        self.assertEqual(asyncio.run(scenario()), dogproto.PREEMPTED)
        self.stack.wait_for(lambda: any(m["name"] == "left" for _, m in self.stack.ops("hold")), "stick hold")


class PoseFeeder(threading.Thread):
    """Stands in for mediad: turns the fake toy pose into robot.observe with a goal marker."""

    def __init__(self, stack, goal):
        super().__init__(daemon=True)
        self.stack = stack
        self.goal = goal
        self.halt = threading.Event()

    def run(self):
        asyncio.run(self._feed())

    async def _feed(self):
        client = await dogproto.Client.connect(self.stack.sock("robotd"))
        path = os.path.join(self.stack.run_dir, "sim_pose.json")
        try:
            while not self.halt.is_set():
                try:
                    with open(path) as handle:
                        pose = json.load(handle)
                except (OSError, ValueError):
                    pose = None
                if pose:
                    client.notify("robot.observe", {"t_mono_ns": time.monotonic_ns(), "markers": {
                        "1": [pose["x"], pose["y"], pose["heading"]], "2": [self.goal[0], self.goal[1], 0.0]}})
                await asyncio.sleep(0.1)
        finally:
            await client.close()


class SimGoto(unittest.TestCase):
    """Simulated goto: 9 of 10 random goals reached."""

    def test_goto_random_goals(self):
        rng = random.Random(7)
        reached = 0
        with Stack() as stack:
            stack.call("robot.enable")
            for seed in range(10):
                pose = stack.sim_pose()
                angle = rng.uniform(-math.pi, math.pi)
                dist = rng.uniform(100, 220)
                goal = (min(600, max(40, pose["x"] + dist * math.cos(angle))),
                        min(440, max(40, pose["y"] + dist * math.sin(angle))))
                feeder = PoseFeeder(stack, goal)
                feeder.start()
                time.sleep(0.3)
                try:
                    result = stack.call("robot.goto", {"marker": 2, "id": aid()}, cloud=True, timeout=40)
                finally:
                    feeder.halt.set()
                    feeder.join()
                reached += result["reached"]
                print(f"\ngoto {seed}: {result['reason']} in {result['steps']} steps")
        self.assertGreaterEqual(reached, 9)


if __name__ == "__main__":
    unittest.main()
