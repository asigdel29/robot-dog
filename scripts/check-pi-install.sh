#!/usr/bin/env bash
# check-pi-install.sh: installs a real release in a Debian 13 container, then checks the commit and a rollback.
# CI runs it as root: docker run --rm -v "$PWD":/repo:ro -w /repo debian:trixie bash scripts/check-pi-install.sh <tarball>
# The hooks, robotctl and robotd are real. The container has no systemd, so shims replace systemctl and systemd-run.
set -euo pipefail

tarball=$(realpath "$1")
# fail prints a reason and stops the check.
fail() {
	echo "check-pi-install: FAIL: $*" >&2
	exit 1
}

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq systemd procps python3 >/dev/null
useradd robot

cat >/usr/local/sbin/systemd-run <<'EOF'
#!/bin/sh
# systemd-run shim: runs the command after "--" as if it were in a unit.
while [ "$1" != "--" ]; do shift; done
shift
INVOCATION_ID=shim exec "$@"
EOF
cat >/usr/local/sbin/systemctl <<'EOF'
#!/bin/sh
# systemctl shim: a restart starts robotd as user dog in the background; enable goes to the real systemctl.
case "$*" in
daemon-reload) ;;
"restart dog.target")
	pkill -f /pi/robotd.py || true
	sleep 0.5
	setsid runuser -u dog -- /opt/dog/current/.venv/bin/python /opt/dog/current/pi/robotd.py \
		--config /etc/dog/dog.toml >>/var/log/robotd.log 2>&1 </dev/null &
	;;
"show robotd -p MainPID -p NRestarts") printf 'MainPID=%s\nNRestarts=0\n' "$(pgrep -o -f /pi/robotd.py || echo 0)" ;;
*) exec /usr/bin/systemctl "$@" ;;
esac
EOF
chmod 0755 /usr/local/sbin/systemd-run /usr/local/sbin/systemctl

echo "== first install"
bash scripts/install-pi.sh "$tarball" >/var/log/install.log 2>&1 || {
	tail -n 20 /var/log/install.log
	fail "install-pi.sh"
}
version=$(tar -xzOf "$tarball" VERSION)
[[ $(readlink /opt/dog/current) == "releases/$version" ]] || fail "$version is not current"
[[ $(runuser -u robot -- robotctl state) =~ \"health\":\ \"(ok|degraded)\" ]] || fail "robotd health"
[[ $(id -nG dog) == *doglink* && $(id -nG robot) == *dog* ]] || fail "group membership"
[[ $(stat -c '%U:%G %a' /etc/dog/registry.json /etc/dog/link.env /etc/dog) == $'root:dog 664\ndoglink:doglink 600\nroot:dog 2775' ]] ||
	fail "/etc/dog owners and modes"
[[ $(stat -c '%U:%G %a' /run/dog/robotd.sock /run/dog/robotd-cloud.sock) == $'dog:dog 660\ndog:doglink 660' ]] ||
	fail "socket owners and modes"

echo "== a release whose robotd stops at start must roll back in 40 s or less"
mkdir /root/broken
tar -xzf "$tarball" -C /root/broken
echo broken-1 >/root/broken/VERSION
echo "raise SystemExit('rollback test')" >/root/broken/pi/robotd.py
broken=/root/dog-broken-1.tar.gz
tar -czf "$broken" -C /root/broken .
sha256sum "$broken" >"$broken.sha256"
start=$SECONDS
if robotctl update apply "$broken"; then
	fail "the broken release committed"
fi
elapsed=$((SECONDS - start))
((elapsed <= 40)) || fail "the rollback took $elapsed s"
[[ $(readlink /opt/dog/current) == "releases/$version" ]] || fail "$version is not current after the rollback"
sleep 1
[[ $(runuser -u robot -- robotctl state) =~ \"health\":\ \"(ok|degraded)\" ]] || fail "robotd health after the rollback"
echo "check-pi-install: PASS: $version committed; the broken release rolled back in $elapsed s"
