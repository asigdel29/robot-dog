#!/usr/bin/env bash
# install-pi.sh: first install on the Pi. It installs the apt packages, then applies the first release.
# Run it once on the Pi: sudo ./install-pi.sh dog-<version>.tar.gz (the .sha256 file must be beside it).
# The Pi runs Raspberry Pi OS Lite 64-bit based on Debian 13 Trixie, for OpenCV 4.7 or later.
# The release hooks do every other change to the board, so later releases need only deploy-pi.sh.
set -euo pipefail

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
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tar -xzf "$tarball" -C "$work" pi hooks

# The apt list is the APT_PACKAGES line of hooks/preinstall, so the hook checks the same packages.
read -ra packages <<<"$(sed -n 's/^APT_PACKAGES=(\(.*\))$/\1/p' "$work/hooks/preinstall")"
if ((${#packages[@]} == 0)); then
	echo "install-pi.sh: no APT_PACKAGES line in hooks/preinstall" >&2
	exit 1
fi
apt-get update
apt-get install -y python3 "${packages[@]}"

# No release is current yet, so run robotctl from the new tarball with the system python3 (it needs only the stdlib).
python3 "$work/pi/robotctl.py" update apply "$tarball"
