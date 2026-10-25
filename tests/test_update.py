"""Tests of `robotctl update`: digest check, health gate, rollback, prune and status.

Each test uses a temp DOG_ROOT, PATH shims for systemctl and systemd-run, and a fake
robotd on a temp DOG_RUN_DIR. The fake answers robot.state with a health value that
comes from the VERSION of the current release: "ok-1" answers ok, "fault-2" answers
fault and "silent-2" never answers. DOG_FAKE_VENV skips the real venv and pip step.
"""

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))

import dogproto  # noqa: E402

ROBOTCTL = os.path.join(ROOT, "pi", "robotctl.py")
GATE_S = 3.0

SYSTEMCTL_SHIM = '#!/bin/sh\necho "systemctl $*" >> "$SHIM_LOG"\n'
SYSTEMD_RUN_SHIM = '''#!/bin/sh
echo "systemd-run $*" >> "$SHIM_LOG"
while [ "$1" != "--" ]; do shift; done
shift
INVOCATION_ID=shim exec "$@"
'''
HOOK = '#!/bin/sh\necho "{name} {version}" >> "$HOOK_LOG"\n'


class FakeRobotd(threading.Thread):
    """A robotd stand-in whose health follows the VERSION of the current release."""

    def __init__(self, sock, current):
        super().__init__(daemon=True)
        self.sock = sock
        self.current = current
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()

    def run(self):
        """Serve robot.state until stop() is called."""
        asyncio.set_event_loop(self.loop)
        server = self.loop.run_until_complete(dogproto.serve(self.sock, {"robot.state": self.h_state}))
        self.ready.set()
        self.loop.run_forever()
        server.close()
        tasks = asyncio.all_tasks(self.loop)
        for task in tasks:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        self.loop.run_until_complete(asyncio.sleep(0))
        self.loop.close()

    async def h_state(self, params, conn):
        """Answer with the health named by the first part of the current VERSION, or never."""
        with open(os.path.join(self.current, "VERSION")) as handle:
            health = handle.read().strip().split("-")[0]
        if health == "silent":
            await asyncio.Event().wait()
        return {"health": health}

    def stop(self):
        """Stop the event loop thread."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.join(5)


class UpdateTests(unittest.TestCase):
    """robotctl update apply, rollback and status against shims and a fake robotd."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.dog = os.path.join(base, "root", "opt", "dog")
        run_dir = os.path.join(base, "run")
        shims = os.path.join(base, "shims")
        for path in (self.dog, run_dir, shims):
            os.makedirs(path)
        for name, text in (("systemctl", SYSTEMCTL_SHIM), ("systemd-run", SYSTEMD_RUN_SHIM)):
            with open(os.path.join(shims, name), "w") as handle:
                handle.write(text)
            os.chmod(os.path.join(shims, name), 0o755)
        self.shim_log = os.path.join(base, "shim.log")
        self.hook_log = os.path.join(base, "hook.log")
        self.env = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
        self.env.update(DOG_ROOT=os.path.join(base, "root"), DOG_RUN_DIR=run_dir, DOG_CONFIG=os.devnull,
                        DOG_FAKE_VENV="1", DOG_HEALTH_WAIT_S=str(GATE_S), SHIM_LOG=self.shim_log,
                        HOOK_LOG=self.hook_log, PATH=shims + os.pathsep + os.environ["PATH"])
        self.robotd = FakeRobotd(os.path.join(run_dir, "robotd.sock"), os.path.join(self.dog, "current"))
        self.robotd.start()
        self.robotd.ready.wait(5)

    def tearDown(self):
        self.robotd.stop()
        self.tmp.cleanup()

    def release(self, version, digest_ok=True, failing_hook=None):
        """Write dog-<version>.tar.gz with stub hooks and its .sha256 file; return the tarball path."""
        tarball = os.path.join(self.tmp.name, f"dog-{version}.tar.gz")
        members = {"VERSION": (version + "\n", 0o644), "pi/robotd.py": ("", 0o644)}
        for name in ("preinstall", "postinstall"):
            text = HOOK.format(name=name, version=version)
            members[f"hooks/{name}"] = (text + ("exit 1\n" if name == failing_hook else ""), 0o755)
        with tarfile.open(tarball, "w:gz") as tar:
            for name, (text, mode) in members.items():
                data = text.encode()
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
        with open(tarball, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        with open(f"{tarball}.sha256", "w") as handle:
            handle.write(f"{digest if digest_ok else '0' * 64}  {os.path.basename(tarball)}\n")
        return tarball

    def robotctl(self, *args):
        """Run robotctl as a child process and return it."""
        return subprocess.run([sys.executable, ROBOTCTL, *args], env=self.env, capture_output=True, text=True,
                              timeout=60)

    def apply(self, version, **kwargs):
        """Run `update apply` for a new release of that version."""
        return self.robotctl("update", "apply", self.release(version, **kwargs))

    def link(self, name):
        """Return the release name of the current or previous symlink, or None."""
        path = os.path.join(self.dog, name)
        return os.path.basename(os.readlink(path)) if os.path.islink(path) else None

    def releases(self):
        """Return the release directories on disk."""
        return sorted(os.listdir(os.path.join(self.dog, "releases")))

    def lines(self, path):
        """Return the lines of a log file, or [] when it is absent."""
        if not os.path.exists(path):
            return []
        with open(path) as handle:
            return handle.read().splitlines()

    def test_good_release_commits(self):
        proc = self.apply("ok-1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["health"], "ok")
        self.assertEqual(self.link("current"), "ok-1")
        self.assertIsNone(self.link("previous"))
        self.assertEqual(self.lines(self.hook_log), ["preinstall ok-1", "postinstall ok-1"])
        self.assertIn("systemctl restart dog.target", self.lines(self.shim_log))
        self.assertTrue(any(line.startswith("systemd-run --unit=dog-update --wait --collect --pipe")
                            for line in self.lines(self.shim_log)))
        self.assertTrue(os.path.islink(os.path.join(self.dog, "releases", "ok-1", ".venv", "bin", "python")))
        self.assertEqual(os.stat(os.path.join(self.dog, "releases", "ok-1")).st_mode & 0o777, 0o755)
        proc = self.apply("degraded-2")  # hardware absent is not a reason to roll back
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.link("current"), self.link("previous")), ("degraded-2", "ok-1"))

    def test_bad_digest_refused_before_any_change(self):
        self.assertEqual(self.apply("ok-1").returncode, 0)
        before = (self.lines(self.shim_log), self.lines(self.hook_log))
        proc = self.apply("ok-2", digest_ok=False)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("sha256 mismatch", proc.stderr)
        self.assertEqual(self.link("current"), "ok-1")
        self.assertEqual(self.releases(), ["ok-1"])
        self.assertEqual(self.lines(self.hook_log), before[1])
        restarts = [line for line in self.lines(self.shim_log) if line.startswith("systemctl")]
        self.assertEqual(restarts, [line for line in before[0] if line.startswith("systemctl")])

    def assert_rolled_back(self, version):
        """Apply a bad release after ok-1 and check that ok-1 runs again inside the gate time."""
        self.assertEqual(self.apply("ok-1").returncode, 0)
        start = time.monotonic()
        proc = self.apply(version)
        elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("rolled back to ok-1", proc.stderr)
        self.assertLess(elapsed, GATE_S + 4)
        self.assertEqual(self.link("current"), "ok-1")
        self.assertIsNone(self.link("previous"))
        self.assertEqual(self.releases(), ["ok-1"])
        self.assertEqual(self.lines(self.hook_log)[-2:], [f"postinstall {version}", "postinstall ok-1"])
        restarts = [line for line in self.lines(self.shim_log) if line == "systemctl restart dog.target"]
        self.assertEqual(len(restarts), 3)
        return elapsed

    def test_fault_rolls_back_at_once(self):
        self.assertLess(self.assert_rolled_back("fault-2"), GATE_S)

    def test_no_answer_rolls_back_after_gate(self):
        self.assertGreaterEqual(self.assert_rolled_back("silent-2"), GATE_S)

    def test_failed_preinstall_changes_nothing(self):
        self.assertEqual(self.apply("ok-1").returncode, 0)
        proc = self.apply("ok-2", failing_hook="preinstall")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.link("current"), "ok-1")
        self.assertEqual(self.releases(), ["ok-1"])

    def test_failed_postinstall_rolls_back(self):
        self.assertEqual(self.apply("ok-1").returncode, 0)
        proc = self.apply("ok-2", failing_hook="postinstall")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("rolled back to ok-1", proc.stderr)
        self.assertEqual((self.link("current"), self.releases()), ("ok-1", ["ok-1"]))

    def test_previous_version_is_refused(self):
        for version in ("ok-1", "ok-2"):
            self.assertEqual(self.apply(version).returncode, 0)
        proc = self.apply("ok-1")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("update rollback", proc.stderr)
        self.assertEqual(self.releases(), ["ok-1", "ok-2"])

    def test_prune_keeps_one_previous(self):
        for version in ("ok-1", "ok-2", "ok-3"):
            self.assertEqual(self.apply(version).returncode, 0)
        self.assertEqual(self.releases(), ["ok-2", "ok-3"])
        self.assertEqual((self.link("current"), self.link("previous")), ("ok-3", "ok-2"))

    def test_rollback_command(self):
        self.assertEqual(self.robotctl("update", "rollback").returncode, 3)  # nothing to roll back to
        for version in ("ok-1", "ok-2"):
            self.assertEqual(self.apply(version).returncode, 0)
        proc = self.robotctl("update", "rollback")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.link("current"), self.link("previous")), ("ok-1", "ok-2"))
        self.assertEqual(self.releases(), ["ok-1", "ok-2"])

    def test_status(self):
        for version in ("ok-1", "ok-2"):
            self.assertEqual(self.apply(version).returncode, 0)
        reexecs = len(self.lines(self.shim_log))
        proc = self.robotctl("update", "status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"current": "ok-2", "previous": "ok-1", "releases": ["ok-1", "ok-2"]})
        self.assertEqual(len(self.lines(self.shim_log)), reexecs)  # status reads only, so no systemd-run


if __name__ == "__main__":
    unittest.main()
