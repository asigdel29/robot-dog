#!/usr/bin/env bash
# install-pi.sh: first install on the Pi. It installs the apt packages, then applies the first release.
# Run it once on the Pi: sudo ./install-pi.sh dog-<version>.tar.gz (the .sha256 file must be beside it).
# The Pi runs Raspberry Pi OS Lite 64-bit based on Debian 13 Trixie, for OpenCV 4.7 or later.
# The release hooks do every other change to the board, so later releases need only deploy-pi.sh.
set -euo pipefail

# Keep this list in step with APT_PACKAGES in hooks/preinstall.
APT_PACKAGES=(python3 python3-venv python3-opencv python3-numpy python3-evdev alsa-utils)

if [[ $# -ne 1 ]]; then
	echo "usage: sudo $0 dog-<version>.tar.gz" >&2
	exit 3
fi
if [[ $EUID -ne 0 ]]; then
	echo "install-pi.sh: run it as root (sudo)" >&2
	exit 3
fi
tarball=$(realpath "$1")
(cd "$(dirname "$tarball")" && sha256sum --check --quiet "$(basename "$tarball").sha256")

apt-get update
apt-get install -y "${APT_PACKAGES[@]}"

# No release is current yet, so run robotctl from the new tarball with the system python3 (it needs only the stdlib).
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tar -xzf "$tarball" -C "$work" pi
python3 "$work/pi/robotctl.py" update apply "$tarball"
