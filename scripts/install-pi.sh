#!/usr/bin/env bash
# install-pi.sh: first install on the Pi. It installs the apt packages, then applies the first release.
# Usage on the Pi: sudo bash install-pi.sh dog-<version>.tar.gz (the .sha256 file must be beside it).
# The release hooks make every other change to the board, so later releases need only deploy-pi.sh.
set -euo pipefail

tarball=$(realpath "${1:?usage: sudo bash install-pi.sh dog-<version>.tar.gz}")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tar -xzf "$tarball" -C "$work" pi hooks
# The apt list is the APT_PACKAGES line of hooks/preinstall, so the hook checks the same packages.
read -ra packages <<<"$(sed -n 's/^APT_PACKAGES=(\(.*\))$/\1/p' "$work/hooks/preinstall")"
apt-get update
apt-get install -y "${packages[@]}"
# No release is current yet, so robotctl runs from the tarball with the system python3 (it needs only the stdlib).
python3 "$work/pi/robotctl.py" update apply "$tarball"
