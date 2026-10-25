# Releases and updates

This document owns the release format, the update procedure, the health gate and the rollback rules of the Pi software. When this document and the code disagree, correct this document first. Then correct the code. The code is `pi/robotctl.py` (the `update` commands), `hooks/`, `scripts/package.sh` and `deploy/pi/`.

## 1. Release file

`scripts/package.sh` makes two files on the laptop:

- `dist/dog-<version>.tar.gz`: the release.
- `dist/dog-<version>.tar.gz.sha256`: its digest, in `sha256sum` format.

The version comes from `git describe --tags --always --dirty`. A version that ends in `-dirty` contains changes that are not in a commit.

The tarball contains these items at its top level:

| Item | Content |
| --- | --- |
| `pi/` | The daemons and `robotctl`. |
| `hooks/` | `preinstall` and `postinstall`. |
| `deploy/pi/` | The units, the tmpfiles entry, the `robotctl` launcher, the env templates and `requirements.txt`. |
| `config/` | The templates of `dog.toml` and `registry.json`. |
| `VERSION` | The version, on one line. |

The file list comes from `git ls-files`, in name order. A file that git does not track is not in the release, so add a new file to git before you make a release. The tarball never contains tests, a venv or `__pycache__`. Each member has owner 0, group 0, the time of the last commit and mode 0644 or 0755. Thus the same tree always gives the same bytes.

## 2. Layout on the Pi

| Path | Content | Writer |
| --- | --- | --- |
| `/opt/dog/releases/<version>/` | One release, with its own `.venv`. | `robotctl update` |
| `/opt/dog/current` | Symlink to `releases/<version>`. The units start the daemons from this path. | `robotctl update` |
| `/opt/dog/previous` | Symlink to the release before `current`. | `robotctl update` |
| `/usr/local/bin/robotctl` | Launcher that runs `robotctl` of the current release. | `postinstall` |
| `/etc/systemd/system/dog.target` and the four units | Copies from `deploy/pi/`. | `postinstall` |
| `/etc/tmpfiles.d/dog.conf` | Creates `/run/dog`, `/var/lib/dog` and `/var/lib/dog/captures`. | `postinstall` |
| `/etc/modules-load.d/dog.conf` | Loads the drivers that register the device numbers of the dog hardware at boot (section 3). | `postinstall` |
| `/opt/dog/.lock` | The lock of `robotctl update`. | `robotctl update` |
| `/etc/dog/` | `dog.toml`, `registry.json`, `link.env`, `console.env`. | The owner and `robotctl` |

The files in `/etc/dog/` and `/var/lib/dog/` are outside the release. An update never replaces them. `postinstall` copies a template only when the target file is absent.

Each release venv uses `python3 -m venv --system-site-packages`. Thus the apt packages `python3-opencv`, `python3-numpy` and `python3-evdev` are available in the venv. Pip installs only the pinned packages in `deploy/pi/requirements.txt`.

## 3. Users and file modes

| Item | Value | Why |
| --- | --- | --- |
| User `dog` | Groups `dialout`, `video`, `audio`, `input`, `doglink` | Runs robotd, mediad and padd. It must be in `doglink` to give the cloud sockets that group. |
| User `doglink` | No device groups | Runs linkd, the only process with internet access. |
| Admin `robot` | Group `dog` | Runs `robotctl`, which writes the registry and the captures. |
| `/run/dog` | `dog:dog`, 0755 | Socket directory. Each daemon sets the group of its own sockets. |
| `/etc/dog` | `root:dog`, 2775 | `robotctl` replaces `registry.json` with a rename in this directory. |
| `registry.json` | `root:dog`, 0664 at the first copy | `robotctl` is its only writer. After a write, the writer owns the file and the group stays `dog`. |
| `link.env` | `doglink:doglink`, 0600 | The device token of linkd. |
| `console.env` | `root:dog`, 0640 | The LAN console token of mediad. |
| `/var/lib/dog/captures` | `dog:dog`, 2770 | Raw IR captures from `robotctl registry capture`. |

`preinstall` creates the groups and users before the daemons start. A daemon that cannot set the group of a socket makes the socket owner-only, so the order is important.

Each unit has `DevicePolicy=closed`. Thus a daemon can open only the standard pseudo devices (for example `/dev/null` and the ptys) and the devices in this table:

| Unit | Devices | Other limits |
| --- | --- | --- |
| robotd | `char-ttyUSB rw`, `char-ttyACM rw` (the IR bridge) | Unix sockets only. |
| mediad | `char-video4linux rw`, `char-alsa rw` (camera, microphone, speaker) | `SystemCallFilter=@system-service`. The filter blocks ptrace, so mediad cannot reach into robotd. |
| padd | `char-input r` (the gamepad) | Unix sockets only. `SystemCallFilter=@system-service`. |
| linkd | None (`PrivateDevices=yes`) | No access to a device group. |

A `char-<driver>` rule finds its device major in `/proc/devices` when the unit starts. A driver that loads after that time stays blocked until the unit starts again. Thus `/etc/modules-load.d/dog.conf` loads `usbserial`, `cdc_acm`, `videodev` and `snd` at boot. A container test showed the effect: with no `ttyUSB` driver, robotd's rules give EPERM for a major-188 node.

## 4. Update procedure

`robotctl update apply <tarball>` does these steps in this sequence:

1. When `INVOCATION_ID` is not set, it starts itself again in the transient unit `dog-update` with `systemd-run --unit=dog-update --wait --collect --pipe`. A lost SSH session then cannot stop the update halfway. Only `DOG_ROOT`, `DOG_RUN_DIR`, `DOG_FAKE_VENV` and `DOG_HEALTH_WAIT_S` go into the unit. A token never does.
2. It takes an exclusive lock on `/opt/dog/.lock`. A second update stops at once with exit code 1.
3. It reads the digest from `<tarball>.sha256` and compares it with the tarball. A mismatch stops the update before any change.
4. It extracts the tarball into a temporary directory in `releases/`. It refuses links, absolute paths and `..` in member names.
5. It reads `VERSION`. It refuses the version of `current` or `previous`. Then it renames the directory to `releases/<version>`.
6. It runs `hooks/preinstall` of the new release.
7. It makes the venv and installs the pinned packages.
8. It points `previous` at the old release and `current` at the new release. Each change is a new symlink and a rename, so it is atomic.
9. It runs `hooks/postinstall` of the new release.
10. It runs `systemctl restart dog.target`.
11. It applies the health gate (section 5). Then it commits the release or does a rollback (section 6).
12. It deletes each release that is not `current` or `previous`.

A failure in steps 6 or 7 deletes the new release. The current release does not change.

`robotctl` captures the output of each child process. It prints the output only when the process fails. A lost terminal thus cannot stop a hook or pip. Each child process has a time limit: 300 s for a hook or `systemctl`, and 900 s for pip. A child process that does not finish in time is a failure.

`robotctl` writes the result to the journal with the tag `robotctl`. After a lost SSH session, run `journalctl -t robotctl` or `robotctl update status`.

`robotd.service` has `TimeoutStartSec=10` and `TimeoutStopSec=5`. Thus a robotd that does not start or stop cannot hold the restart for a long time.

## 5. Health gate

After the restart, `robotctl` sends `robot.state` to robotd each second. The gate lasts 30 s or less.

| Event | Result |
| --- | --- |
| `ok` or `degraded` for 10 s, with no change of robotd `MainPID` and `NRestarts` | Commit. |
| `fault` at any time | Rollback at once. |
| No answer after a good answer | Rollback at once. |
| A new robotd `MainPID` or `NRestarts` after the first good answer | Rollback at once. |
| No good answer in the first 20 s | Rollback at 30 s, because the 10 s settle window cannot end in the gate. |

`degraded` counts as good. Absent hardware, for example no IR bridge, gives `degraded`. This is the microduck rule: a release is good when its daemons run, also with no hardware.

The settle window is 10 s, which is longer than `WatchdogSec=2`. Thus a robotd that stops in its loop gets a watchdog restart in the window, and the new `MainPID` stops the commit. `robotctl` reads `MainPID` and `NRestarts` with `systemctl show robotd`. A good release thus commits approximately 10 s after the restart.

The gate checks only robotd, because robotd owns the safety rules. A daemon that stops and starts again does not move the dog: each start is DISARMED.

## 6. Rollback

A failed gate, a failed `postinstall`, a failed restart, or a stop of the update (for example SIGTERM) causes these steps:

1. `current` points at the old release again, and `previous` gets its old target again.
2. `hooks/postinstall` of the old release runs again. It puts back the units of the old release.
3. `systemctl restart dog.target` runs.
4. Step 12 of section 4 deletes the failed release.
5. `robotctl` writes the reason to the journal and stops with exit code 1.

`robotctl` changes the first SIGTERM into an exit that does the rollback first. It ignores a second SIGTERM, so that the rollback can complete.

After a broken release, the old release is current again in approximately 32 s: the 30 s gate, the restart and the hook. The phase 6 limit is 40 s.

The first install has no old release. When it fails, `robotctl` removes `current`, stops `dog.target` and deletes the new release. Thus you can apply the same tarball again after you correct the fault.

`robotctl update rollback` makes the previous release current, with the same gate. When that gate fails, the release that was current before the command becomes current again.

## 7. Hooks

`robotctl` runs each hook as root, from the release directory. A hook finds its release from its own path.

- `preinstall` checks Python 3.11 or later, the apt packages, `cv2.aruco.ArucoDetector` (OpenCV 4.7 or later) and 200 MiB of free space in `/opt`. It creates the absent groups and users and sets their group membership. It changes nothing when all are present.
- `postinstall` copies the units, the tmpfiles entry, the modules-load file and the launcher. It runs `systemctl daemon-reload` and `systemd-tmpfiles --create`. It creates `/etc/dog` and copies each absent template. It enables `dog.target`. It does not start a daemon.

Microduck rule: each change that `scripts/install-pi.sh` makes to the board is also in a hook. `install-pi.sh` installs the apt packages and applies the first release with `robotctl`. It reads the package list from the `APT_PACKAGES` line of `hooks/preinstall`, so the list is in one place.

## 8. Exit codes and test values

| Exit code | Result |
| --- | --- |
| 0 | The release is current and the gate passed. |
| 1 | The update did not commit, or another update is active. The message names the release that is current. |
| 3 | Bad input: a bad digest, an unsafe tarball, a bad `VERSION`, the version of `current` or `previous`, or no previous release. |

The tests in `tests/test_update.py` use these environment values. The production units never set them.

| Name | Effect |
| --- | --- |
| `DOG_ROOT` | Prefix for `/opt/dog`. |
| `DOG_RUN_DIR` | Directory of the robotd socket. |
| `DOG_FAKE_VENV` | Makes a venv that is only a link to the test Python. There is no pip step. |
| `DOG_HEALTH_WAIT_S` | Gate time, 30 s by default. The settle window is 10 s or a third of the gate time, whichever is shorter. |

The tests put shims for `systemctl` and `systemd-run` first in `PATH`. The `systemd-run` shim gives the command only the `--setenv` values, as a transient unit does.

`scripts/check-pi-install.sh` installs a real release in a Debian 13 container, with real hooks and a real robotd. Then it applies a broken release and measures the rollback. CI runs it in the `deploy` job.

## 9. Microduck parts that are not in this design

| Microduck part | Why it is not here | When to add it |
| --- | --- | --- |
| minisign release signatures | Releases arrive over SSH from an admin user who has root access. A signature adds no protection against that user. | When updates come from a public URL or a third party. |
| `golden` release | The SD card holds the full system, and the owner can write it again with Pi Imager. | When the Pi is far from the owner. |
| Boot counter | A failed release is found by the health gate, not by a boot loop. The update does not change the kernel or the boot files. | When a release changes the boot files or the kernel. |
| Rescue timer | `robotctl` does the rollback itself in the `dog-update` unit, and that unit survives a lost SSH session. | When an update can stop the network of the Pi, or when updates run with no person present. |
