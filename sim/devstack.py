"""Run the dog stack on one machine against the fake ESP32, for tests and local work.

``python sim/devstack.py`` starts fake_esp32 and robotd with DEMO_REGISTRY in a
temporary run directory and prints how to reach them. Tests import Stack. Each
process gets DOG_RUN_DIR, so nothing touches /run/dog or /etc/dog.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))

import dogproto  # noqa: E402


def _nec(cmd, **extra):
    return {"ir": {"proto": "NEC", "addr": 0, "cmd": cmd, "bits": 32, "raw_us": [], "carrier_khz": 38},
            "repeat": "nec", "period_ms": 108, "verified": True, **extra}


#: Verified commands for the fake toy. Real IR codes come from robotctl registry capture.
DEMO_REGISTRY = {
    "schema": 1,
    "robot": {"model": "supireo-y8359", "id": "dog-01"},
    "commands": {
        "forward": _nec(0x18, kind="hold", max_run_ms=1000, cloud=True, say="walks forward"),
        "back": _nec(0x52, kind="hold", max_run_ms=1000, cloud=True, say="walks back"),
        "left": _nec(0x08, kind="hold", max_run_ms=1000, cloud=True, say="turns left"),
        "right": _nec(0x5A, kind="hold", max_run_ms=1000, cloud=True, say="turns right"),
        "bark": _nec(0x45, kind="episodic", repeat="none", natural_ms=1500, cloud=True, say="barks"),
        "dance": _nec(0x46, kind="perpetual", repeat="none", stop_via="stop", natural_ms=3000,
                      max_run_ms=3000, cloud=True, say="dances"),
        "stop": _nec(0x40, kind="episodic", repeat="none", natural_ms=0, say="stops"),
        "program": _nec(0x44, kind="mode", repeat="none", say="program mode"),
    },
    "drive": {"forward": "forward", "back": "back", "left": "left", "right": "right"},
    "goto": {"robot_marker": 1, "goal_radius_px": 40, "area_px": [[0, 0], [640, 0], [640, 480], [0, 480]],
             "turn_ms": 400, "forward_ms": 500, "settle_ms": 200, "turn_sign": 1},
}


class Stack:
    """Processes of one dev stack and helpers to call them and read the fake log."""

    def __init__(self, registry=None):
        self.run_dir = tempfile.mkdtemp(prefix="dog-")
        self.registry_path = os.path.join(self.run_dir, "registry.json")
        with open(self.registry_path, "w") as handle:
            json.dump(DEMO_REGISTRY if registry is None else registry, handle)
        self.env = {**os.environ, "DOG_RUN_DIR": self.run_dir, "PYTHONUNBUFFERED": "1"}
        self.procs = {}
        self._log_pos = 0
        self._log_rows = []

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def spawn(self, name, *args):
        """Start one process; its output goes to <run_dir>/<name>.out."""
        with open(os.path.join(self.run_dir, f"{name}.out"), "w") as log:
            self.procs[name] = subprocess.Popen([sys.executable, *args], env=self.env, stdout=log, stderr=log)

    def start(self):
        """Start fake_esp32 and robotd and wait until the bridge link is up."""
        self.spawn("fake_esp32", os.path.join(ROOT, "sim", "fake_esp32.py"), "--run-dir", self.run_dir)
        self.wait_for(lambda: os.path.exists(os.path.join(self.run_dir, "esp32")), "fake esp32 pty")
        self.start_robotd()

    def start_robotd(self):
        """Start (or start again) robotd and wait for its link to the fake bridge."""
        self.spawn("robotd", os.path.join(ROOT, "pi", "robotd.py"), "--serial", os.path.join(self.run_dir, "esp32"),
                   "--registry", self.registry_path, "--config", os.path.join(self.run_dir, "absent.toml"))
        self.wait_for(lambda: self._state("esp") == "DISARMED", "robotd esp32 link")

    def _state(self, field):
        """Return one field of robot.state, or None while robotd does not answer."""
        try:
            return self.call("robot.state")[field]
        except (OSError, dogproto.RpcError, TimeoutError):
            return None

    def wait_for(self, predicate, what, timeout=10.0):
        """Poll predicate until it is true; raise TimeoutError with what."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.05)
        raise TimeoutError(f"timed out waiting for {what}")

    def sock(self, service, cloud=False):
        """Return a socket path of this stack."""
        return os.path.join(self.run_dir, f"{service}{'-cloud' if cloud else ''}.sock")

    def call(self, method, params=None, cloud=False, timeout=5.0, service="robotd"):
        """Make one request to a daemon of this stack."""
        return asyncio.run(dogproto.call_once(self.sock(service, cloud), method, params, timeout))

    def sim_pose(self):
        """Return the simulated toy pose written by the fake bridge."""
        with open(os.path.join(self.run_dir, "sim_pose.json")) as handle:
            return json.load(handle)

    def log(self):
        """Return the fake bridge log as (t_mono_ns, "in"|"out", message) tuples.

        It parses only the new lines: a full parse on each poll takes enough CPU on
        a small CI runner to delay the daemons under test.
        """
        with open(os.path.join(self.run_dir, "fake_esp32.log"), "rb") as handle:
            handle.seek(self._log_pos)
            complete = handle.read().rpartition(b"\n")[0]
        if complete:
            self._log_pos += len(complete) + 1
            for line in complete.decode().splitlines():
                t_ns, direction, msg = line.split(" ", 2)
                self._log_rows.append((int(t_ns), direction, json.loads(msg)))
        return list(self._log_rows)

    def ops(self, op, since_ns=0):
        """Return logged ops of one kind sent to the fake after since_ns."""
        return [(t, m) for t, d, m in self.log() if d == "in" and m.get("op") == op and t >= since_ns]

    def wait_ops(self, op, count, since_ns=0, timeout=1.0):
        """Wait until the fake has logged count ops of one kind; a reply can arrive before the log line."""
        end = time.monotonic() + timeout
        while True:
            rows = self.ops(op, since_ns)
            if len(rows) >= count or time.monotonic() > end:
                return rows
            time.sleep(0.02)

    def kill(self, name, sig=signal.SIGKILL):
        """Send a signal to one process and wait for it to exit."""
        proc = self.procs[name]
        proc.send_signal(sig)
        proc.wait(5)

    def stop(self):
        """Stop every process of the stack."""
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs.values():
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


def main():
    """Start a dev stack and keep it alive until Ctrl-C."""
    with Stack() as stack:
        print(f"run dir: {stack.run_dir}")
        print(f"robotctl: DOG_RUN_DIR={stack.run_dir} python pi/robotctl.py --config /dev/null state")
        try:
            while all(p.poll() is None for p in stack.procs.values()):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
