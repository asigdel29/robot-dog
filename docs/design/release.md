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
| `registry.json` | `robot:dog`, 0664 | `robotctl` is its only writer. |
| `link.env` | `doglink:doglink`, 0600 | The device token of linkd. |
| `console.env` | `root:dog`, 0640 | The LAN console token of mediad. |
| `/var/lib/dog/captures` | `dog:dog`, 2770 | Raw IR captures from `robotctl registry capture`. |

`preinstall` creates the groups and users before the daemons start. A daemon that cannot set the group of a socket makes the socket owner-only, so the order is important.

## 4. Update procedure

`robotctl update apply <tarball>` does these steps in this sequence:

1. When `INVOCATION_ID` is not set, it starts itself again in the transient unit `dog-update` with `systemd-run --unit=dog-update --wait --collect --pipe`. A lost SSH session then cannot stop the update halfway.
2. It reads the digest from `<tarball>.sha256` and compares it with the tarball. A mismatch stops the update before any change.
3. It extracts the tarball into a temporary directory in `releases/`. It refuses links, absolute paths and `..` in member names.
4. It reads `VERSION`. It refuses the version of `current` or `previous`. Then it renames the directory to `releases/<version>`.
5. It runs `hooks/preinstall` of the new release.
6. It makes the venv and installs the pinned packages.
7. It points `previous` at the old release and `current` at the new release. Each change is a new symlink and a rename, so it is atomic.
8. It runs `hooks/postinstall` of the new release.
9. It runs `systemctl restart dog.target`.
10. It applies the health gate (section 5). Then it commits the release or does a rollback (section 6).
11. It deletes each release that is not `current` or `previous`.

A failure in steps 5 or 6 deletes the new release. The current release does not change.

`robotctl` captures the output of each child process. It prints the output only when the process fails. A lost terminal thus cannot stop a hook or pip. After a lost SSH session, run `robotctl update status` to see the release that is current.

The unit name `dog-update` is also a lock. `systemd-run` refuses a second update while one is active.

## 5. Health gate

After the restart, `robotctl` sends `robot.state` to robotd each second for 30 s. It uses the `health` field.

| Answer | Result |
| --- | --- |
| `ok` | Commit. |
| `degraded` | Commit. Absent hardware, for example no IR bridge, gives `degraded`. This is the microduck rule: a release is good when its daemons run, also with no hardware. |
| `fault` | Rollback at once. |
| No answer in 30 s | Rollback. |

The gate checks only robotd, because robotd owns the safety rules. A daemon that stops and starts again does not move the dog: each start is DISARMED.

## 6. Rollback

A failed gate, a failed `postinstall` or a failed restart causes these steps:

1. `current` points at the old release again, and `previous` gets its old target again.
2. `hooks/postinstall` of the old release runs again. It puts back the units of the old release.
3. `systemctl restart dog.target` runs.
4. Step 11 of section 4 deletes the failed release.
5. `robotctl` prints the reason and stops with exit code 1.

After a broken release, the old release is current again in approximately 32 s: the 30 s gate, the restart and the hook. The phase 6 limit is 40 s.

The first install has no old release. When its gate fails, the new release stays current and `robotctl` stops with exit code 1.

`robotctl update rollback` makes the previous release current, with the same gate. When that gate fails, the release that was current before the command becomes current again.

## 7. Hooks

`robotctl` runs each hook as root, from the release directory. A hook finds its release from its own path.

- `preinstall` checks Python 3.11 or later, the apt packages, `cv2.aruco.ArucoDetector` (OpenCV 4.7 or later) and 200 MiB of free space in `/opt`. It creates the absent groups and users and sets their group membership. It changes nothing when all are present.
- `postinstall` copies the units, the tmpfiles entry and the launcher. It runs `systemctl daemon-reload` and `systemd-tmpfiles --create`. It creates `/etc/dog` and copies each absent template. It enables `dog.target`. It does not start a daemon.

Microduck rule: each change that `scripts/install-pi.sh` makes to the board is also in a hook. `install-pi.sh` installs the apt packages and applies the first release with `robotctl`. `preinstall` checks that the same apt packages are present.

## 8. Exit codes and test values

| Exit code | Result |
| --- | --- |
| 0 | The release is current and the gate passed. |
| 1 | The update did not commit. The message names the release that is current. |
| 3 | Bad input: a bad digest, an unsafe tarball, a bad `VERSION`, the version of `current` or `previous`, or no previous release. |

The tests in `tests/test_update.py` use these environment values. The production units never set them.

| Name | Effect |
| --- | --- |
| `DOG_ROOT` | Prefix for `/opt/dog`. |
| `DOG_RUN_DIR` | Directory of the robotd socket. |
| `DOG_FAKE_VENV` | Makes a venv that is only a link to the test Python. There is no pip step. |
| `DOG_HEALTH_WAIT_S` | Gate time, 30 s by default. |

The tests put shims for `systemctl` and `systemd-run` first in `PATH`. `robotctl` copies each `DOG_*` value into the `dog-update` unit with `--setenv`.

## 9. Microduck parts that are not in this design

| Microduck part | Why it is not here | When to add it |
| --- | --- | --- |
| minisign release signatures | Releases arrive over SSH from an admin user who has root access. A signature adds no protection against that user. | When updates come from a public URL or a third party. |
| `golden` release | The SD card holds the full system, and the owner can write it again with Pi Imager. | When the Pi is far from the owner. |
| Boot counter | A failed release is found by the health gate, not by a boot loop. The update does not change the kernel or the boot files. | When a release changes the boot files or the kernel. |
| Rescue timer | `robotctl` does the rollback itself in the `dog-update` unit, and that unit survives a lost SSH session. | When an update can stop the network of the Pi, or when updates run with no person present. |
