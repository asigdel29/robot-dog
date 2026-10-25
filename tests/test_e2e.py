"""End-to-end tests of the dog stack on one machine, against the fake ESP32.

Each check maps to a row of the verification table in the plan: stop latency,
watchdog after a robotd crash, cloud refusals, arm rules, duplicates, the
simulated goto (with a pose feeder and with mediad's fake camera) and the pose
rate and age. Numbers are printed so a run can go into docs/test-log.md.
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

#: Shared CI machines (2 cores for a private repo) stall a process for 100-300 ms now and then.
#: There the latency gates only catch a gross regression: a stop path that waits for the 300 ms
#: lease shows above 250 ms. Local and hardware runs use the plan targets.
ON_CI = bool(os.environ.get("CI"))


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
        quantiles = statistics.quantiles(samples, n=100)
        p95, p99 = quantiles[94], quantiles[98]
        print(f"\nstop latency ms: p50={statistics.median(samples):.2f} p95={p95:.2f} p99={p99:.2f} max={max(samples):.2f}")
        if ON_CI:
            self.assertLessEqual(p95, 250.0)
        else:
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
        last_hb = max(t for t, _ in self.stack.ops("hb") if t < killed)
        print(f"\nafter kill: last repeat {(last_tx - killed) / 1e6:.0f} ms, fault {(fault - killed) / 1e6:.0f} ms"
              f" ({(fault - last_hb) / 1e6:.0f} ms after the last heartbeat)")
        self.assertGreater(last_tx, killed, "no repeat frame after the kill: the hold was not running")
        self.assertLessEqual((last_tx - killed) / 1e6, 400)
        # Watchdog 1000 ms after the last heartbeat, then the 40 ms gap and the ~70 ms stop frame, then FAULT:
        # about 1110 ms by design, plus slack for process scheduling on a busy CI runner.
        self.assertLessEqual((fault - last_hb) / 1e6, 1300)

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

        delays = []
        for _ in range(5):
            closed = asyncio.run(close_during_drive())
            stops = self.stack.wait_ops("stop", 1, since_ns=closed)
            self.assertTrue(stops, "robotd kept the hold after its owner closed")
            delays.append((stops[0][0] - closed) / 1e6)
        print(f"\nowner close to stop ms: {', '.join(f'{d:.1f}' for d in delays)}")
        if ON_CI:
            self.assertLess(statistics.median(delays), 250)
        else:
            self.assertLess(max(delays), 100)

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


def random_goal(rng, pose, margin=40):
    """Return a goal 100 to 220 px from the toy pose and at least margin px inside the 640 x 480 floor."""
    angle = rng.uniform(-math.pi, math.pi)
    dist = rng.uniform(100, 220)
    return (min(640 - margin, max(margin, pose["x"] + dist * math.cos(angle))),
            min(480 - margin, max(margin, pose["y"] + dist * math.sin(angle))))


class SimGoto(unittest.TestCase):
    """Simulated goto: 9 of 10 random goals reached."""

    def test_goto_random_goals(self):
        rng = random.Random(7)
        reached = 0
        with Stack() as stack:
            stack.call("robot.enable")
            for seed in range(10):
                goal = random_goal(rng, stack.sim_pose())
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


class CameraPose(unittest.TestCase):
    """mediad's fake camera draws the markers and the real ArUco detection finds them."""

    @classmethod
    def setUpClass(cls):
        cls.stack = Stack(media=True)
        cls.stack.start()

    @classmethod
    def tearDownClass(cls):
        cls.stack.stop()

    def test_goto_random_goals_camera(self):
        """9 of 10 random goals reached with poses from the camera path.

        The goals stay 80 px inside the image: the detector drops a marker whose
        white margin touches the image edge, on a real camera too.
        """
        rng = random.Random(7)
        reached = 0
        self.stack.call("robot.enable")
        for seed in range(10):
            goal = random_goal(rng, self.stack.sim_pose(), margin=80)
            self.stack.set_goal_markers({"2": list(goal)})
            time.sleep(0.3)
            result = self.stack.call("robot.goto", {"marker": 2, "id": aid()}, cloud=True, timeout=40)
            reached += result["reached"]
            print(f"\ncamera goto {seed}: {result['reason']} in {result['steps']} steps")
        self.assertGreaterEqual(reached, 9)

    def test_pose_rate_and_age(self):
        """Pose rate 10 Hz or more and vision age p95 under 200 ms, from robot.state samples."""

        async def sample(seconds):
            client = await dogproto.Client.connect(self.stack.sock("robotd"))
            ages = []
            try:
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    ages.append((await client.call("robot.state"))["vision_age_ms"])
                    await asyncio.sleep(0.005)
            finally:
                await client.close()
            return ages

        seconds = 3.0
        ages = asyncio.run(sample(seconds))
        rate = sum(1 for before, after in zip(ages, ages[1:]) if after < before) / seconds
        p95 = statistics.quantiles(ages, n=20)[18]
        print(f"\npose rate {rate:.1f} Hz, vision age p50={statistics.median(ages):.0f} ms p95={p95:.0f} ms "
              f"max={max(ages)} ms ({len(ages)} samples)")
        self.assertGreaterEqual(rate, 10)
        self.assertLess(p95, 200)


if __name__ == "__main__":
    unittest.main()
