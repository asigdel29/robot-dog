#!/usr/bin/env bash
# deploy-pi.sh: builds a release on the laptop, copies it to the Pi and applies it with the health gate.
# Usage: scripts/deploy-pi.sh [ssh-host]   (default: robot-pi)
# robotctl on the Pi runs the update in its own systemd unit, so a lost SSH session cannot stop it.
set -euo pipefail

host=${1:-robot-pi}
tarball=$("$(dirname "$0")/package.sh")
name=$(basename "$tarball")
scp "$tarball" "$tarball.sha256" "$host:/tmp/"
# shellcheck disable=SC2029 # $name expands on the laptop on purpose.
ssh "$host" sudo robotctl update apply "/tmp/$name"
