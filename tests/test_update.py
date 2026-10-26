"""Tests of `robotctl update` (digest, health gate, rollback, prune, status) and of the unit ExecStart lines.

Each update test uses a temp DOG_ROOT, PATH shims for systemctl and systemd-run, and a fake
robotd on a temp DOG_RUN_DIR. The fake answers robot.state with the health that the VERSION
of the current release names:

- "ok-N" answers ok and "degraded-N" answers degraded.
- "fault-N" answers fault and "silent-N" never answers.
- "late-N" answers degraded once, then fault.
- "pidchange-N" answers ok, but robotd gets a new MainPID after the first answer.

DOG_FAKE_VENV skips the real venv and pip step. DOG_HEALTH_WAIT_S=3 gives a 1 s settle window.
"""

import asyncio
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
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
SETTLE_S = GATE_S / 3

SYSTEMCTL_SHIM = '''#!/bin/sh
echo "systemctl $*" >> "$DOG_ROOT/shim.log"
case "$*" in
  "show robotd -p MainPID -p NRestarts")
    if [ -e "$DOG_ROOT/robotd.show" ]; then cat "$DOG_ROOT/robotd.show"; else printf 'MainPID=100\\nNRestarts=0\\n'; fi ;;
  "restart dog.target")
    if [ -e "$DOG_ROOT/fail-restart" ]; then exit 1; fi ;;
esac
'''
SYSTEMD_RUN_SHIM = '''#!{python}
"""systemd-run shim: log the arguments, then run the command with only the --setenv values, like a unit."""
import os, sys
args = sys.argv[1:]
with open(os.path.join(os.environ["DOG_ROOT"], "shim.log"), "a") as log:
    log.write("systemd-run " + " ".join(args) + "\\n")
sep = args.index("--")
env = {{"PATH": os.path.dirname(os.path.abspath(__file__)) + ":/usr/bin:/bin", "INVOCATION_ID": "shim"}}
env.update(a[len("--setenv="):].split("=", 1) for a in args[:sep] if a.startswith("--setenv="))
os.execve(args[sep + 1], args[sep + 1:], env)
'''
#: A stub hook. It sends SIGTERM to robotctl when the file term-<name>-<version> exists.
HOOK = ('#!/bin/sh\necho "{name} {version}" >> "$DOG_ROOT/hook.log"\n'
        '[ ! -e "$DOG_ROOT/term-{name}-{version}" ] || kill -TERM $PPID\n')


class FakeRobotd(threading.Thread):
    """A robotd stand-in whose health follows the VERSION of the current release."""

    def __init__(self, sock, root):
        super().__init__(daemon=True)
        self.sock = sock
        self.root = root
        self.calls = {}
        self.ready = threading.Event()

    def run(self):
        """Serve robot.state until stop(); asyncio.run then cancels each open request and closes the loop."""
        asyncio.run(self.serve())

    async def serve(self):
        """Open the socket and wait for stop()."""
        self.loop, self.stopped = asyncio.get_running_loop(), asyncio.Event()
        server = await dogproto.serve(self.sock, {"robot.state": self.h_state})
        self.ready.set()
        await self.stopped.wait()
        server.close()

    async def h_state(self, params, conn):
        """Answer with the health that the current VERSION names (see the module docstring)."""
        with open(os.path.join(self.root, "opt", "dog", "current", "VERSION")) as handle:
            version = handle.read().strip()
        kind = version.split("-")[0]
        self.calls[version] = count = self.calls.get(version, 0) + 1
        if kind == "silent":
            await asyncio.Event().wait()
        if kind == "late":
            return {"health": "degraded" if count == 1 else "fault"}
        if kind == "pidchange":
            if count >= 2:
                with open(os.path.join(self.root, "robotd.show"), "w") as handle:
                    handle.write("MainPID=200\nNRestarts=1\n")
            return {"health": "ok"}
        return {"health": kind}

    def stop(self):
        """Stop the event loop thread."""
        self.loop.call_soon_threadsafe(self.stopped.set)
        self.join(5)


class UpdateTests(unittest.TestCase):
    """robotctl update apply, rollback and status against shims and a fake robotd."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.root = os.path.join(base, "root")
        self.dog = os.path.join(self.root, "opt", "dog")
        run_dir = os.path.join(base, "run")
        shims = os.path.join(base, "shims")
        for path in (self.dog, run_dir, shims):
            os.makedirs(path)
        shim_text = {"systemctl": SYSTEMCTL_SHIM, "systemd-run": SYSTEMD_RUN_SHIM.format(python=sys.executable)}
        for name, text in shim_text.items():
            with open(os.path.join(shims, name), "w") as handle:
                handle.write(text)
            os.chmod(os.path.join(shims, name), 0o755)
        self.shim_log = os.path.join(self.root, "shim.log")
        self.hook_log = os.path.join(self.root, "hook.log")
        self.env = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
        self.env.update(DOG_ROOT=self.root, DOG_RUN_DIR=run_dir, DOG_FAKE_VENV="1",
                        DOG_HEALTH_WAIT_S=str(GATE_S), DOG_DEVICE_TOKEN="secret-token-value",
                        PATH=shims + os.pathsep + os.environ["PATH"])
        self.robotd = FakeRobotd(os.path.join(run_dir, "robotd.sock"), self.root)
        self.robotd.start()
        self.robotd.ready.wait(5)

    def tearDown(self):
        self.robotd.stop()
        self.tmp.cleanup()

    def release(self, version, digest_ok=True, failing_hook=None, unsafe=None):
        """Write dog-<version>.tar.gz with stub hooks and its .sha256 file; return the tarball path.

        unsafe adds one more member: ("symlink", name) or ("file", name).
        """
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
            if unsafe and unsafe[0] == "symlink":
                info = tarfile.TarInfo(unsafe[1])
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(info)
            elif unsafe:
                info = tarfile.TarInfo(unsafe[1])
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
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

    def restarts(self):
        """Return how many times robotctl restarted dog.target."""
        return self.lines(self.shim_log).count("systemctl restart dog.target")

    def test_good_release_commits(self):
        start = time.monotonic()
        proc = self.apply("ok-1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreaterEqual(time.monotonic() - start, SETTLE_S)  # no commit on the first answer
        self.assertEqual(json.loads(proc.stdout)["health"], "ok")
        self.assertIn("update committed", proc.stderr)
        self.assertEqual(self.link("current"), "ok-1")
        self.assertIsNone(self.link("previous"))
        self.assertEqual(self.lines(self.hook_log), ["preinstall ok-1", "postinstall ok-1"])
        self.assertEqual(self.restarts(), 1)
        reexec = [line for line in self.lines(self.shim_log) if line.startswith("systemd-run ")]
        self.assertEqual(len(reexec), 1)
        self.assertTrue(reexec[0].startswith("systemd-run --unit=dog-update --wait --collect --pipe "))
        self.assertNotIn("secret-token-value", reexec[0])  # only the listed DOG_* values go into the unit
        release = os.path.join(self.dog, "releases", "ok-1")
        self.assertTrue(os.path.islink(os.path.join(release, ".venv", "bin", "python")))
        self.assertEqual(os.stat(release).st_mode & 0o777, 0o755)
        proc = self.apply("degraded-2")  # absent hardware is not a reason for a rollback
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.link("current"), self.link("previous")), ("degraded-2", "ok-1"))

    def test_bad_digest_refused_before_any_change(self):
        self.assertEqual(self.apply("ok-1").returncode, 0)
        hooks_before = self.lines(self.hook_log)
        proc = self.apply("ok-2", digest_ok=False)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("sha256 mismatch", proc.stderr)
        self.assertEqual((self.link("current"), self.releases()), ("ok-1", ["ok-1"]))
        self.assertEqual(self.lines(self.hook_log), hooks_before)
        self.assertEqual(self.restarts(), 1)

    def test_unsafe_members_refused(self):
        self.assertEqual(self.apply("ok-1").returncode, 0)
        for version, unsafe in (("ok-2", ("symlink", "pi/link")), ("ok-3", ("file", "../escape"))):
            proc = self.apply(version, unsafe=unsafe)
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertIn("unsafe tar member", proc.stderr)
        self.assertEqual((self.link("current"), self.releases()), ("ok-1", ["ok-1"]))
        self.assertFalse(os.path.exists(os.path.join(self.dog, "escape")))
        self.assertEqual(self.restarts(), 1)

    def assert_rolled_back(self, version, restarts=3, **kwargs):
        """Apply a bad release after ok-1; check that ok-1 is current again within the gate time."""
        self.assertEqual(self.apply("ok-1").returncode, 0)
        start = time.monotonic()
        proc = self.apply(version, **kwargs)
        elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("rolled back to ok-1", proc.stderr)
        self.assertLess(elapsed, GATE_S + 4)
        self.assertEqual((self.link("current"), self.link("previous")), ("ok-1", None))
        self.assertEqual(self.releases(), ["ok-1"])
        self.assertEqual(self.lines(self.hook_log)[-1], "postinstall ok-1")
        self.assertEqual(self.restarts(), restarts)
        return proc, elapsed

    def test_late_fault_rolls_back(self):
        proc, elapsed = self.assert_rolled_back("late-2")
        self.assertIn("health fault", proc.stderr)
        self.assertLess(elapsed, GATE_S)

    def test_robotd_restart_in_gate_rolls_back(self):
        proc, _ = self.assert_rolled_back("pidchange-2")
        self.assertIn("robotd started again", proc.stderr)

    def test_no_answer_rolls_back_after_gate(self):
        self.assertGreaterEqual(self.assert_rolled_back("silent-2")[1], GATE_S)

    def test_failed_postinstall_rolls_back(self):
        self.assert_rolled_back("ok-2", restarts=2, failing_hook="postinstall")

    def test_failed_restart_rolls_back(self):
        self.assertEqual(self.apply("ok-1").returncode, 0)
        open(os.path.join(self.root, "fail-restart"), "w").close()
        proc = self.apply("ok-2")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("systemctl failed", proc.stderr)
        self.assertEqual((self.link("current"), self.releases()), ("ok-1", ["ok-1"]))

    def test_sigterm_rolls_back(self):
        self.assertEqual(self.apply("ok-1").returncode, 0)
        for name in ("term-postinstall-ok-2", "term-postinstall-ok-1"):  # SIGTERM in the update and in each rollback
            open(os.path.join(self.root, name), "w").close()
        for version, reason in (("ok-2", "stopped by SIGTERM"), ("fault-3", "health fault")):
            proc = self.apply(version)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn(f"{version}: {reason}; rolled back to ok-1", proc.stderr)
        self.assertEqual((self.link("current"), self.releases()), ("ok-1", ["ok-1"]))
        self.assertEqual(self.restarts(), 4)  # each rollback ran to its end

    def test_failed_preinstall_changes_nothing(self):
        self.assertEqual(self.apply("ok-1").returncode, 0)
        proc = self.apply("ok-2", failing_hook="preinstall")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual((self.link("current"), self.releases()), ("ok-1", ["ok-1"]))
        self.assertEqual(self.restarts(), 1)

    def test_failed_first_install_can_run_again(self):
        for _ in range(2):  # the second run must not stop at "already the current release"
            proc = self.apply("fault-1")
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("no release is current", proc.stderr)
            self.assertIsNone(self.link("current"))
            self.assertEqual(self.releases(), [])
        self.assertIn("systemctl disable --now dog.target", self.lines(self.shim_log))
        self.assertEqual(self.apply("ok-1").returncode, 0)
        self.assertEqual(self.link("current"), "ok-1")

    def test_previous_version_is_refused(self):
        for version in ("ok-1", "ok-2"):
            self.assertEqual(self.apply(version).returncode, 0)
        proc = self.apply("ok-1")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("update rollback", proc.stderr)
        self.assertEqual(self.releases(), ["ok-1", "ok-2"])

    def test_second_update_is_refused_while_one_is_active(self):
        with open(os.path.join(self.dog, ".lock"), "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            proc = self.apply("ok-1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("another update is active", proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dog, "releases")))

    def test_prune_keeps_one_previous(self):
        for version in ("ok-1", "ok-2", "ok-3"):
            self.assertEqual(self.apply(version).returncode, 0)
        self.assertEqual(self.releases(), ["ok-2", "ok-3"])
        self.assertEqual((self.link("current"), self.link("previous")), ("ok-3", "ok-2"))

    def test_rollback_and_status(self):
        self.assertEqual(self.robotctl("update", "rollback").returncode, 3)  # nothing to roll back to
        for version in ("ok-1", "ok-2"):
            self.assertEqual(self.apply(version).returncode, 0)
        proc = self.robotctl("update", "rollback")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        shim_lines = len(self.lines(self.shim_log))
        proc = self.robotctl("update", "status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"current": "ok-1", "previous": "ok-2", "releases": ["ok-1", "ok-2"]})
        self.assertEqual(len(self.lines(self.shim_log)), shim_lines)  # status only reads, so no systemd-run


class UnitExecStart(unittest.TestCase):
    """Each unit ExecStart gives arguments that the daemon's own argparse accepts."""

    UNITS = [os.path.join(ROOT, "deploy", "pi", name)
             for name in ("robotd.service", "mediad.service", "padd.service", "linkd.service")]
    UNITS.append(os.path.join(ROOT, "deploy", "hub", "doghub.service"))
    #: Runs the daemon until its argparse parses; exit 0 = accepted, 2 = refused, 77 = import failed.
    PROBE = (
        "import argparse, os, runpy, sys\n"
        "class Parsed(Exception): pass\n"
        "parse = argparse.ArgumentParser.parse_args\n"
        "def probe(self, args=None, namespace=None):\n"
        "    parse(self, args, namespace)\n"
        "    raise Parsed\n"
        "argparse.ArgumentParser.parse_args = probe\n"
        "sys.path.insert(0, os.path.dirname(sys.argv[1]))\n"
        "sys.argv = sys.argv[1:]\n"
        "try:\n"
        "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
        "except Parsed:\n"
        "    sys.exit(0)\n"
        "except ImportError as err:\n"
        "    print(err)\n"
        "    sys.exit(77)\n"
        "sys.exit(99)\n"
    )

    def test_exec_start_arguments_parse(self):
        for unit in self.UNITS:
            with open(unit) as handle:
                line = next(x for x in handle if x.startswith("ExecStart="))
            argv = shlex.split(line[len("ExecStart="):])
            script = re.sub(r"^/opt/dog/current/|^/opt/doghub/", "", argv[1])
            path = os.path.join(ROOT, script)
            with self.subTest(unit=os.path.basename(unit)):
                if not os.path.exists(path):
                    self.skipTest(f"{script} is not in this tree")
                if len(argv) == 2:
                    continue  # the unit gives no arguments
                proc = subprocess.run([sys.executable, "-c", self.PROBE, path, *argv[2:]], capture_output=True,
                                      text=True, timeout=30)
                if proc.returncode == 77:
                    self.skipTest(f"{script} needs a package that is not installed: {proc.stdout.strip()}")
                self.assertEqual(proc.returncode, 0, f"{script} refused {argv[2:]}: {proc.stderr[-500:]}")


if __name__ == "__main__":
    unittest.main()
