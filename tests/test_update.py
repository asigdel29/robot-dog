"""Tests of `robotctl update` (health gate, own unit).

Each update test uses a temp DOG_ROOT, PATH shims for systemctl and systemd-run, and a fake
robotd on a temp DOG_RUN_DIR. The fake answers robot.state with the health that the VERSION
of the current release names:

- "ok-N" answers ok and "degraded-N" answers degraded.

DOG_FAKE_VENV skips the real venv and pip step. DOG_HEALTH_WAIT_S=3 gives a 1 s settle window.
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
SETTLE_S = GATE_S / 3

SYSTEMCTL_SHIM = '''#!/bin/sh
echo "systemctl $*" >> "$DOG_ROOT/shim.log"
case "$*" in
  "show robotd -p MainPID -p NRestarts")
    if [ -e "$DOG_ROOT/robotd.show" ]; then cat "$DOG_ROOT/robotd.show"; else printf 'MainPID=100\\nNRestarts=0\\n'; fi ;;
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


if __name__ == "__main__":
    unittest.main()
