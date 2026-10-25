#!/usr/bin/env bash
# check-pi-install.sh: installs a real release in a Debian 13 container and checks the commit and the rollback.
# CI runs it as root in debian:trixie: docker run --rm -v "$PWD":/repo:ro -w /repo debian:trixie \
#   bash scripts/check-pi-install.sh dist/dog-<version>.tar.gz
# The hooks, robotctl and robotd are real. A container has no systemd, so shims stand in for systemctl and
# systemd-run: the systemctl shim starts robotd as user dog, and the systemd-run shim runs the command directly.
set -euo pipefail

tarball=$(realpath "$1")
fail() {
	echo "check-pi-install: FAIL: $*" >&2
	exit 1
}

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq systemd procps python3 >/dev/null
id -u robot >/dev/null 2>&1 || useradd --create-home robot

cat >/usr/local/sbin/systemd-run <<'EOF'
#!/bin/sh
# systemd-run shim: run the command after "--" as if it were in a unit.
while [ "$1" != "--" ]; do shift; done
shift
INVOCATION_ID=shim exec "$@"
EOF
cat >/usr/local/sbin/systemctl <<'EOF'
#!/bin/sh
# systemctl shim: robotd runs as user dog in the background; unit file commands go to the real systemctl.
case "$*" in
daemon-reload) ;;
"restart dog.target" | "stop dog.target")
	pkill -f /pi/robotd.py || true
	sleep 0.5
	if [ "$1" = restart ]; then
		setsid runuser -u dog -- /opt/dog/current/.venv/bin/python /opt/dog/current/pi/robotd.py \
			--config /etc/dog/dog.toml >>/var/log/robotd.log 2>&1 </dev/null &
	fi ;;
"show robotd -p MainPID -p NRestarts")
	echo "MainPID=$(pgrep -o -f /pi/robotd.py || echo 0)"
	echo "NRestarts=0" ;;
*) exec /usr/bin/systemctl "$@" ;;
esac
EOF
chmod 0755 /usr/local/sbin/systemd-run /usr/local/sbin/systemctl

echo "== first install"
cp "$tarball" "$tarball.sha256" /root/
first=/root/$(basename "$tarball")
bash scripts/install-pi.sh "$first" >/var/log/install.log 2>&1 || {
	tail -n 20 /var/log/install.log
	fail "install-pi.sh"
}
version=$(tar -xzOf "$first" VERSION)
[[ $(robotctl update status) == *"\"current\": \"$version\""* ]] || fail "$version is not current"
[[ $(runuser -u robot -- robotctl state) =~ \"health\":\ \"(ok|degraded)\" ]] || fail "robotd health"
[[ $(id -nG dog) == *doglink* && $(id -nG robot) == *dog* ]] || fail "group membership"
[[ $(stat -c '%U:%G %a' /etc/dog/registry.json /etc/dog/link.env /etc/dog) == $'root:dog 664\ndoglink:doglink 600\nroot:dog 2775' ]] ||
	fail "/etc/dog owners and modes"
[[ $(stat -c '%U:%G %a' /run/dog/robotd.sock /run/dog/robotd-cloud.sock) == $'dog:dog 660\ndog:doglink 660' ]] ||
	fail "socket owners and modes"

echo "== a release whose robotd stops at start must roll back in 40 s or less"
broken=/root/dog-broken-1.tar.gz
python3 - "$first" "$broken" <<'EOF'
import hashlib, io, os, sys, tarfile
src, out = sys.argv[1], sys.argv[2]
with tarfile.open(src) as old, tarfile.open(out, "w:gz") as new:
    for member in old.getmembers():
        data = old.extractfile(member).read()
        data = {"VERSION": b"broken-1\n", "pi/robotd.py": b"raise SystemExit('rollback test')\n"}.get(member.name, data)
        member.size = len(data)
        new.addfile(member, io.BytesIO(data))
with open(out, "rb") as handle, open(out + ".sha256", "w") as digest:
    digest.write(hashlib.sha256(handle.read()).hexdigest() + "  " + os.path.basename(out) + "\n")
EOF
start=$SECONDS
if robotctl update apply "$broken"; then
	fail "the broken release committed"
fi
elapsed=$((SECONDS - start))
((elapsed <= 40)) || fail "the rollback took $elapsed s"
[[ $(robotctl update status) == *"\"current\": \"$version\""* ]] || fail "$version is not current after the rollback"
sleep 1
[[ $(runuser -u robot -- robotctl state) =~ \"health\":\ \"(ok|degraded)\" ]] || fail "robotd health after the rollback"
echo "check-pi-install: PASS: $version committed; the broken release rolled back in $elapsed s"
