"""Run the dog stack on one machine against the fake ESP32, for tests and local work.

``python sim/devstack.py`` starts fake_esp32, robotd with DEMO_REGISTRY, and
mediad with its fake camera, fake audio and the LAN console on 127.0.0.1, in a
temporary run directory, and prints how to reach them. Tests import Stack. Each
process gets DOG_RUN_DIR, so nothing touches /run/dog or /etc/dog.
"""

import asyncio
import json
import os
import secrets
import signal
import socket
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


def free_port():
    """Return a TCP port on 127.0.0.1 that is free now, for the console of one stack."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Stack:
    """Processes of one dev stack and helpers to call them and read the fake logs.

    media=True also starts mediad. The robotd tests leave it out, so the camera
    work does not share the CPU with their latency checks.
    """

    def __init__(self, registry=None, pose="320,240,0", media=False):
        self.run_dir = tempfile.mkdtemp(prefix="dog-")
        self.registry_path = os.path.join(self.run_dir, "registry.json")
        with open(self.registry_path, "w") as handle:
            json.dump(DEMO_REGISTRY if registry is None else registry, handle)
        self.pose = pose
        self.media = media
        self.console_port = free_port()
        self.console_token = secrets.token_hex(16)
        self.env = {**os.environ, "DOG_RUN_DIR": self.run_dir, "PYTHONUNBUFFERED": "1",
                    "DOG_CONSOLE_TOKEN": self.console_token}
        self.procs = {}
        self._logs = []
        self._log_pos = 0
        self._log_rows = []

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def spawn(self, name, *args):
        """Start one process; its output goes to <run_dir>/<name>.out."""
        log = open(os.path.join(self.run_dir, f"{name}.out"), "w")
        self._logs.append(log)
        self.procs[name] = subprocess.Popen([sys.executable, *args], env=self.env, stdout=log, stderr=log)
        return self.procs[name]

    def start(self):
        """Start fake_esp32, robotd and (with media=True) mediad, and wait until each is ready."""
        self.spawn("fake_esp32", os.path.join(ROOT, "sim", "fake_esp32.py"), "--run-dir", self.run_dir,
                   "--pose", self.pose)
        self.wait_for(lambda: os.path.exists(os.path.join(self.run_dir, "esp32")), "fake esp32 pty")
        self.start_robotd()
        if self.media:
            self.start_mediad()

    def start_robotd(self):
        """Start (or start again) robotd and wait for its link to the fake bridge."""
        self.spawn("robotd", os.path.join(ROOT, "pi", "robotd.py"), "--serial", os.path.join(self.run_dir, "esp32"),
                   "--registry", self.registry_path, "--config", os.path.join(self.run_dir, "absent.toml"))
        self.wait_for(lambda: self._state_esp() == "DISARMED", "robotd esp32 link")

    def start_mediad(self):
        """Start mediad with the fake camera and fake audio; wait until robotd sees its camera poses."""
        self.spawn("mediad", os.path.join(ROOT, "pi", "mediad.py"), "--fake-camera", "--fake-audio",
                   "--config", os.path.join(self.run_dir, "absent.toml"),
                   "--console-bind", "127.0.0.1", "--console-port", str(self.console_port))
        self.wait_for(lambda: self._state("vision_age_ms") is not None, "camera poses at robotd")

    def _state(self, field):
        """Return one field of robot.state, or None while robotd does not answer."""
        try:
            return self.call("robot.state")[field]
        except (OSError, dogproto.RpcError, TimeoutError):
            return None

    def _state_esp(self):
        """Return the bridge state that robotd reports, or None."""
        return self._state("esp")

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

    def set_goal_markers(self, markers):
        """Put goal markers on the fake camera floor, for example {"2": [x, y]}; mediad reads them each frame."""
        path = os.path.join(self.run_dir, "fake_markers.json")
        with open(path + ".tmp", "w") as handle:
            json.dump(markers, handle)
        os.replace(path + ".tmp", path)

    def console_url(self, path="/"):
        """Return the URL of a LAN console path of this stack."""
        return f"http://127.0.0.1:{self.console_port}{path}"

    def audio_log(self):
        """Return the fake speaker log as (t_mono_ns, "play"|"end"|"cut", fields) tuples."""
        try:
            with open(os.path.join(self.run_dir, "audio_out.log")) as handle:
                lines = handle.read().splitlines()
        except FileNotFoundError:
            return []
        rows = []
        for line in lines:
            t_ns, event, fields = line.split(" ", 2)
            rows.append((int(t_ns), event, json.loads(fields)))
        return rows

    def log(self):
        """Return the fake bridge log as (t_mono_ns, "in"|"out", message) tuples.

        Reads only the new bytes since the last call: a full re-read on each poll
        takes enough CPU on a small CI runner to delay the daemons under test.
        """
        with open(os.path.join(self.run_dir, "fake_esp32.log"), "rb") as handle:
            handle.seek(self._log_pos)
            data = handle.read()
        complete, _, _ = data.rpartition(b"\n")
        if complete:
            self._log_pos += len(complete) + 1
        for line in complete.decode(errors="replace").splitlines():
            parts = line.split(" ", 2)
            if len(parts) != 3:
                continue
            try:
                self._log_rows.append((int(parts[0]), parts[1], json.loads(parts[2])))
            except ValueError:
                continue
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
        for log in self._logs:
            log.close()


def main():
    """Start a dev stack and keep it alive until Ctrl-C."""
    with Stack(media=True) as stack:
        print(f"run dir: {stack.run_dir}")
        print(f"robotctl: DOG_RUN_DIR={stack.run_dir} python pi/robotctl.py --config /dev/null state")
        print(f"console: {stack.console_url('/?token=' + stack.console_token)}")
        try:
            while all(p.poll() is None for p in stack.procs.values()):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
