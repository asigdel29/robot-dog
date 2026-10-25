"""robotctl: command-line control of the dog on the Pi, and the only writer of the registry.

It talks to robotd over the local socket and prints JSON. From a laptop, run it
over SSH: ``ssh robot-pi robotctl state``. Exit codes: 0 success, 1 the daemon
refused or an update did not commit, 2 the daemon is not reachable, 3 bad input.
The ``update`` commands install, roll back and list releases. The design follows
microduck's robotctl and updaterd.
"""

import argparse
import asyncio
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import syslog
import tarfile
import tempfile
import time
import uuid

import dogproto
import robotd

EXIT_OK, EXIT_REFUSED, EXIT_UNREACHABLE, EXIT_INPUT = 0, 1, 2, 3
TAP_MS = 3000
HOLD_MS = 4000
#: Protocols whose frames carry a toggle bit; a replay of one fixed frame is ignored on the second press.
TOGGLE_PROTOS = ("RC5", "RC6", "RC6A", "MARANTZ")


class InputError(Exception):
    """Bad input from the person at the terminal."""


def rpc(method, params=None, timeout=10.0):
    """Make one request to robotd and return its result."""
    return asyncio.run(dogproto.call_once(dogproto.sock_path("robotd"), method, params, timeout))


def out(value):
    """Print one JSON value for people and scripts."""
    print(json.dumps(value, indent=2))


# ---------------------------------------------------------------- registry file


def read_registry(path):
    """Return the raw registry document (not normalized), or a new empty one."""
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {"schema": 1, "robot": {"model": "supireo-y8359", "id": "dog-01"}, "commands": {},
                "drive": {d: None for d in robotd.DIRS}, "goto": dict(robotd.GOTO_DEFAULTS)}


def write_registry(path, doc):
    """Check the document, then replace the file atomically; refuse a document that breaks a rule."""
    errors = robotd.check_registry(doc)
    if errors:
        raise InputError("registry rules: " + "; ".join(errors))
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def reload_after_write():
    """Ask robotd to read the registry again; an armed dog keeps the old registry until a disarm."""
    try:
        return rpc("robot.reload")
    except dogproto.RpcError as err:
        if err.code != dogproto.BUSY:
            raise
        return {"written": True, "reload": "pending: disarm the dog, then run robotctl registry reload"}


def entry_from_captures(taps, hold):
    """Build a registry entry from three tap captures and one hold capture; raise InputError if they differ."""
    first = taps[0]
    proto = first.get("proto")
    if str(proto).upper() in TOGGLE_PROTOS:
        raise InputError(f"{proto} uses a toggle bit; replay is refused")
    for other in taps[1:]:
        if proto == "NEC":
            if ((other.get("proto"), other.get("addr"), other.get("cmd"), other.get("data"))
                    != ("NEC", first["addr"], first["cmd"], first.get("data"))):
                raise InputError("the three presses decoded differently")
        else:
            a, b = first.get("raw", []), other.get("raw", [])
            if abs(len(a) - len(b)) > 2 or any(abs(x - y) > 0.25 * max(x, y) for x, y in zip(a, b)):
                raise InputError("the three presses have different timings")
    if proto == "NEC":
        ir = {"proto": "NEC", "addr": first["addr"], "cmd": first["cmd"], "bits": first.get("bits", 32),
              "raw_us": [], "carrier_khz": 38}
        if first.get("data") is not None:
            ir["data"] = first["data"]  # the 32 bits on the air: exact replay, also for an extended address
    else:
        ir = {"proto": "raw", "addr": 0, "cmd": 0, "bits": 0, "raw_us": first.get("raw", []), "carrier_khz": 38}
    repeats = [f for f in hold if f.get("repeat")]
    full = [f for f in hold if not f.get("repeat")]
    gaps = [f["gap_us"] for f in hold if f.get("gap_us")]
    if repeats and proto == "NEC":
        repeat = "nec"
    elif len(full) >= 2:
        repeat = "resend"
    else:
        repeat = "none"
    period_ms = int(statistics.median(gaps) / 1000) if gaps else 108
    return {"ir": ir, "kind": "episodic" if repeat == "none" else "hold", "repeat": repeat,
            "period_ms": period_ms, "max_run_ms": 500, "verified": False, "cloud": False,
            "notes": f"captured {datetime.date.today().isoformat()}"}


def capture(name, registry_path, captures_dir):
    """Capture one remote button: three presses and one hold; write it as unverified."""
    taps = []
    for i in range(3):
        print(f"[{i + 1}/3] Press and release the '{name}' button once within {TAP_MS // 1000} s.", file=sys.stderr)
        frames = [f for f in rpc("robot.capture", {"ms": TAP_MS}, TAP_MS / 1000 + 3)["frames"] if not f.get("repeat")]
        if not frames:
            raise InputError("no IR frame seen; check the wires and aim the remote at the receiver")
        taps.append(frames[0])
    print(f"Hold the '{name}' button down for {HOLD_MS // 1000} s.", file=sys.stderr)
    hold = rpc("robot.capture", {"ms": HOLD_MS}, HOLD_MS / 1000 + 3)["frames"]
    entry = entry_from_captures(taps, hold)
    os.makedirs(captures_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    with open(os.path.join(captures_dir, f"{name}-{stamp}.json"), "w") as handle:
        json.dump({"name": name, "taps": taps, "hold": hold}, handle)
    doc = read_registry(registry_path)
    doc.setdefault("commands", {})[name] = entry
    write_registry(registry_path, doc)
    rpc("robot.reload")
    return {"name": name, **entry}


def verify(args, registry_path):
    """Record tested behavior for one command and mark it verified."""
    doc = read_registry(registry_path)
    cmd = doc.get("commands", {}).get(args.name)
    if cmd is None:
        raise InputError(f"no command {args.name}; capture it first")
    changes = {"kind": args.kind, "stop_via": args.stop_via, "natural_ms": args.natural_ms,
               "max_run_ms": args.max_run_ms, "say": args.say}
    for key, value in changes.items():
        if value is not None:
            cmd[key] = value
    if args.interruptible is not None:
        cmd["interruptible"] = args.interruptible == "yes"
    if args.cloud is not None:
        cmd["cloud"] = args.cloud == "yes"
    cmd["verified"] = True
    evidence = args.notes or "verified"
    cmd["notes"] = f"{cmd.get('notes', '')}; {datetime.date.today().isoformat()}: {evidence}".strip("; ")
    write_registry(registry_path, doc)
    return reload_after_write()


def map_drive(direction, name, registry_path):
    """Point one drive direction at a hold command, or clear it with 'none'."""
    if direction not in robotd.DIRS:
        raise InputError(f"direction must be one of {robotd.DIRS}")
    doc = read_registry(registry_path)
    doc.setdefault("drive", {})[direction] = None if name == "none" else name
    write_registry(registry_path, doc)
    return reload_after_write()


# ---------------------------------------------------------------- releases (docs/design/release.md)

#: Longest time of the health gate, in seconds. DOG_HEALTH_WAIT_S changes it for tests.
HEALTH_WAIT_S = 30
#: robotd must stay healthy with one PID for this long before a commit. It is longer than WatchdogSec=2.
SETTLE_S = 10
HOOK_TIMEOUT_S = 300
PIP_TIMEOUT_S = 900
#: The only environment values that go into the dog-update unit. Tokens never do.
FORWARD_ENV = ("DOG_ROOT", "DOG_RUN_DIR", "DOG_FAKE_VENV", "DOG_HEALTH_WAIT_S")


class UpdateError(Exception):
    """An update or rollback that did not commit; the message says which release runs now."""


def dog_root():
    """Return the release root /opt/dog; DOG_ROOT prefixes it for tests."""
    return os.environ.get("DOG_ROOT", "") + "/opt/dog"


def say(text):
    """Print one progress line; a lost SSH terminal must not stop an update halfway."""
    try:
        print(text, file=sys.stderr, flush=True)
    except OSError:
        pass


def report(text, code):
    """Print the outcome of an update and record it in the journal, which keeps it after a lost SSH session."""
    say(text)
    syslog.openlog("robotctl")
    syslog.syslog(syslog.LOG_NOTICE if code == EXIT_OK else syslog.LOG_ERR, text)
    return code


def link_version(name):
    """Return the release name that the current or previous symlink points at, or None."""
    try:
        return os.path.basename(os.readlink(os.path.join(dog_root(), name)))
    except FileNotFoundError:
        return None


def point(name, version):
    """Point a symlink at releases/<version> atomically (new link, then rename); None removes it."""
    link = os.path.join(dog_root(), name)
    if version is None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(link)
        return
    tmp = f"{link}.new"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    os.symlink(os.path.join("releases", version), tmp)
    os.replace(tmp, link)


def quiet(cmd, timeout=HOOK_TIMEOUT_S, **kwargs):
    """Run a command with captured output, so a lost terminal cannot break it; raise UpdateError on failure."""
    name = os.path.basename(cmd[0])
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout,
                              **kwargs)
    except subprocess.TimeoutExpired:
        raise UpdateError(f"{name} did not finish in {timeout} s") from None
    if proc.returncode != 0:
        say(proc.stdout[-2000:])
        raise UpdateError(f"{name} failed with exit code {proc.returncode}")


def check_sha256(tarball):
    """Compare the tarball with the digest in its sibling .sha256 file; raise InputError on a mismatch."""
    try:
        with open(f"{tarball}.sha256") as handle:
            expected = handle.read().split()[0].lower()
        with open(tarball, "rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
    except (OSError, IndexError) as err:
        raise InputError(f"cannot read the release or its .sha256 file: {err}") from None
    if actual != expected:
        raise InputError(f"sha256 mismatch for {tarball}; the file is damaged or incomplete")


def extract(tarball):
    """Extract a release tarball into releases/<VERSION> and return the version."""
    releases = os.path.join(dog_root(), "releases")
    os.makedirs(releases, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".incoming-", dir=releases)
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            for member in tar.getmembers():
                parts = member.name.split("/")
                if not (member.isfile() or member.isdir()) or member.name.startswith("/") or ".." in parts:
                    raise InputError(f"unsafe tar member {member.name}")
            tar.extractall(staging, filter="data")
        with open(os.path.join(staging, "VERSION")) as handle:
            version = handle.read().strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
            raise InputError(f"bad VERSION {version!r}")
        if version == link_version("current"):
            raise InputError(f"{version} is already the current release")
        if version == link_version("previous"):
            raise InputError(f"{version} is the previous release; use robotctl update rollback")
        os.chmod(staging, 0o755)  # mkdtemp makes it 0700; the dog users must read the release
        target = os.path.join(releases, version)
        shutil.rmtree(target, ignore_errors=True)
        os.rename(staging, target)
        return version
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def make_venv(release):
    """Create the release venv on top of the apt packages and pip-install the pinned packages."""
    venv = os.path.join(release, ".venv")
    if os.environ.get("DOG_FAKE_VENV"):  # tests: no network and no pip
        os.makedirs(os.path.join(venv, "bin"))
        os.symlink(sys.executable, os.path.join(venv, "bin", "python"))
        return
    quiet(["python3", "-m", "venv", "--system-site-packages", venv])
    quiet([os.path.join(venv, "bin", "python"), "-m", "pip", "install", "--no-cache-dir",
           "--disable-pip-version-check", "-r", os.path.join(release, "deploy", "pi", "requirements.txt")],
          timeout=PIP_TIMEOUT_S)


def hook(version, name):
    """Run hooks/<name> of a release."""
    release = os.path.join(dog_root(), "releases", version)
    quiet([os.path.join(release, "hooks", name)], cwd=release)


def robotd_identity():
    """Return the MainPID and NRestarts of robotd.service; a change means that robotd started again."""
    try:
        return subprocess.run(["systemctl", "show", "robotd", "-p", "MainPID", "-p", "NRestarts"],
                              capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def health_gate():
    """Return "ok" or "degraded" after SETTLE_S of good answers from one robotd; else raise UpdateError.

    The gate polls robot.state for DOG_HEALTH_WAIT_S (30 s) at most. A fault, a lost answer after a
    good one, or a new robotd MainPID or NRestarts fails at once (docs/design/release.md section 5).
    """
    wait = float(os.environ.get("DOG_HEALTH_WAIT_S", HEALTH_WAIT_S))
    settle = min(SETTLE_S, wait / 3)
    deadline = time.monotonic() + wait
    since, identity = None, None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            health = rpc("robot.state", timeout=min(2.0, remaining)).get("health")
        except (dogproto.RpcError, OSError, TimeoutError):
            health = None
        if health == "fault":
            raise UpdateError("health fault")
        if health in ("ok", "degraded"):
            if since is None:
                since, identity = time.monotonic(), robotd_identity()
            elif robotd_identity() != identity:
                raise UpdateError("robotd started again during the gate")
            if time.monotonic() - since >= settle:
                return health
        elif since is not None:
            raise UpdateError("robotd stopped answering")
        time.sleep(max(0.0, min(1.0, deadline - time.monotonic())))
    raise UpdateError("no stable health" if since is not None else "no answer")


def restore(old, old_previous):
    """Undo a release change: make the old release current again, or leave none current after a first install."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a SIGTERM must not stop a rollback halfway
    if old is None:
        point("current", None)
        with contextlib.suppress(UpdateError, OSError):
            quiet(["systemctl", "disable", "--now", "dog.target"])
        return "no release is current and dog.target is disabled; correct the fault and apply again"
    point("current", old)
    point("previous", old_previous)
    with contextlib.suppress(UpdateError, OSError):
        hook(old, "postinstall")
    with contextlib.suppress(UpdateError, OSError):
        quiet(["systemctl", "restart", "dog.target"])
    return f"rolled back to {old}"


def activate(version):
    """Make a release current, restart dog.target and apply the health gate; on any failure undo the change."""
    old, old_previous = link_version("current"), link_version("previous")
    try:
        point("current", version)
        if old:
            point("previous", old)
        hook(version, "postinstall")
        quiet(["systemctl", "restart", "dog.target"])
        return {"current": version, "previous": old, "health": health_gate()}
    except BaseException as err:  # also a SIGTERM (on_sigterm) and an OSError
        raise UpdateError(f"{version}: {str(err) or type(err).__name__}; {restore(old, old_previous)}") from None


def prune():
    """Delete each release that is not current or previous, so one earlier release stays."""
    keep = {link_version("current"), link_version("previous")}
    releases = os.path.join(dog_root(), "releases")
    for name in os.listdir(releases):
        if name not in keep:
            shutil.rmtree(os.path.join(releases, name), ignore_errors=True)


def update_apply(tarball):
    """Install a release tarball: digest, extract, preinstall, venv, activate with the health gate, prune."""
    check_sha256(tarball)
    version = extract(tarball)
    say(f"extracted {version}")
    try:
        hook(version, "preinstall")
        make_venv(os.path.join(dog_root(), "releases", version))
        return activate(version)
    finally:
        prune()


def update_rollback():
    """Make the previous release current again, with the same health gate."""
    previous = link_version("previous")
    if previous is None:
        raise InputError("no previous release")
    return activate(previous)


def update_status():
    """Return the current and previous release names and each release on disk."""
    releases = os.path.join(dog_root(), "releases")
    names = sorted(n for n in os.listdir(releases) if not n.startswith(".")) if os.path.isdir(releases) else []
    return {"current": link_version("current"), "previous": link_version("previous"), "releases": names}


@contextlib.contextmanager
def update_lock():
    """Hold an exclusive lock on /opt/dog/.lock, so that two updates never run at the same time."""
    os.makedirs(dog_root(), exist_ok=True)
    with open(os.path.join(dog_root(), ".lock"), "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("another update is active") from None
        yield


def on_sigterm(signum, frame):
    """Turn the first SIGTERM into an UpdateError, so that the update stops with a rollback; ignore later ones."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise UpdateError("stopped by SIGTERM")


def in_own_unit(argv):
    """Run this command again in the dog-update systemd unit, so a lost SSH session cannot stop it halfway."""
    env = [f"--setenv={key}={os.environ[key]}" for key in FORWARD_ENV if key in os.environ]
    cmd = ["systemd-run", "--unit=dog-update", "--wait", "--collect", "--pipe", *env,
           "--", sys.executable, os.path.realpath(__file__), *argv]
    return subprocess.run(cmd).returncode


# ---------------------------------------------------------------- CLI


def build_parser():
    """Return the argument parser."""
    parser = argparse.ArgumentParser(prog="robotctl", description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.environ.get("DOG_CONFIG", "/etc/dog/dog.toml"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("state", "enable", "disarm", "stop"):
        sub.add_parser(name)
    sub.add_parser("do").add_argument("trick")
    drive = sub.add_parser("drive")
    drive.add_argument("dir", choices=robotd.DIRS)
    drive.add_argument("ms", type=int)
    sub.add_parser("goto").add_argument("marker", type=int)
    reg = sub.add_parser("registry").add_subparsers(dest="reg", required=True)
    reg.add_parser("list")
    reg.add_parser("reload")
    reg.add_parser("capture").add_argument("name")
    test = reg.add_parser("test")
    test.add_argument("name")
    test.add_argument("--ms", type=int, default=300)
    ver = reg.add_parser("verify")
    ver.add_argument("name")
    ver.add_argument("--kind", choices=robotd.KINDS)
    ver.add_argument("--stop-via")
    ver.add_argument("--natural-ms", type=int)
    ver.add_argument("--max-run-ms", type=int)
    ver.add_argument("--interruptible", choices=("yes", "no"))
    ver.add_argument("--cloud", choices=("yes", "no"))
    ver.add_argument("--say")
    ver.add_argument("--notes")
    mapping = reg.add_parser("map")
    mapping.add_argument("dir")
    mapping.add_argument("name")
    upd = sub.add_parser("update").add_subparsers(dest="update", required=True)
    upd.add_parser("apply").add_argument("tarball")
    upd.add_parser("rollback")
    upd.add_parser("status")
    return parser


def run_update(args):
    """Run one update command and return its exit code; apply and rollback move into their own unit first."""
    if args.update == "status":
        out(update_status())
        return EXIT_OK
    if not os.environ.get("INVOCATION_ID"):
        tail = [os.path.abspath(args.tarball)] if args.update == "apply" else []
        return in_own_unit(["update", args.update, *tail])
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        with update_lock():
            result = update_apply(args.tarball) if args.update == "apply" else update_rollback()
    except InputError as err:
        return report(f"update refused: {err}", EXIT_INPUT)
    except (UpdateError, OSError) as err:
        return report(f"update did not commit: {err}", EXIT_REFUSED)
    report(f"update committed: {result['current']} is current with health {result['health']}", EXIT_OK)
    try:
        out(result)
    except BrokenPipeError:  # the SSH session is gone; the journal has the outcome
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return EXIT_OK


def run(argv):
    """Run one command and return its exit code."""
    args = build_parser().parse_args(argv)
    if args.cmd == "update":
        return run_update(args)
    cfg = robotd.load_config(args.config)
    registry_path = cfg.get("registry", "/etc/dog/registry.json")
    captures_dir = cfg.get("captures", "/var/lib/dog/captures")
    try:
        if args.cmd in ("state", "enable", "disarm", "stop"):
            out(rpc(f"robot.{args.cmd}"))
        elif args.cmd == "do":
            out(rpc("robot.do", {"trick": args.trick, "id": uuid.uuid4().hex}, 30))
        elif args.cmd == "drive":
            out(rpc("robot.drive", {"dir": args.dir, "ms": args.ms, "id": uuid.uuid4().hex}, 10))
        elif args.cmd == "goto":
            out(rpc("robot.goto", {"marker": args.marker, "id": uuid.uuid4().hex}, 40))
        elif args.reg == "list":
            out(read_registry(registry_path).get("commands", {}))
        elif args.reg == "reload":
            out(rpc("robot.reload"))
        elif args.reg == "capture":
            out(capture(args.name, registry_path, captures_dir))
        elif args.reg == "test":
            out(rpc("robot.test", {"name": args.name, "ms": args.ms}, 10))
        elif args.reg == "verify":
            out(verify(args, registry_path))
        elif args.reg == "map":
            out(map_drive(args.dir, args.name, registry_path))
    except dogproto.RpcError as err:
        print(f"refused ({err.code}): {err.message}", file=sys.stderr)
        return EXIT_REFUSED
    except InputError as err:
        print(str(err), file=sys.stderr)
        return EXIT_INPUT
    except (OSError, TimeoutError) as err:  # ConnectionError, FileNotFoundError and PermissionError are OSErrors
        print(f"robotd not reachable: {err}", file=sys.stderr)
        return EXIT_UNREACHABLE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
