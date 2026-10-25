#!/usr/bin/env bash
# deploy-hub.sh: provisions the droplet (Caddy, ufw, the hub, rootless Docker and Hermes Agent) over SSH.
# Usage on the laptop: scripts/deploy-hub.sh <ssh-target> <domain>, for example root@203.0.113.10 example.com.
# It copies hub/ and deploy/hub/ to the host and runs this script there as root with --remote.
# Each step is idempotent. Secrets and config files are created only when they are absent.
set -euo pipefail

# Hermes Agent pin: the release tag, the sha256 of its scripts/install.sh, and the commit of that tag.
# docs/runbook.md, "Update the Hermes pin", tells how to change these three values together.
HERMES_TAG=v0.22.5
HERMES_INSTALLER_SHA256=99cb3691d55019c6973a7acb775a915648666cc24e483ca827a1cdbb3531a674
HERMES_COMMIT=bf0203a794a07aac3ff764972ab9087baa4e4f4e
# The sha256 of /etc/caddy/Caddyfile as the Ubuntu 24.04 caddy package installs it.
UBUNTU_CADDYFILE_SHA256=66177d46fa761acb07208065db9b0274cb1b12c02ac43b9bfc9857b698b1ccfe
DOG_CADDYFILE_HEADER='# Caddyfile: the public TLS front of the hub'

# remote runs on the droplet as root; $1 is the domain and $2 is the copied source directory.
remote() {
	local domain=$1 src=$2
	local hh=/home/hermes/.hermes hubenv=/etc/doghub/hub.env caddyfile=/etc/caddy/Caddyfile
	local uid codename device tools apikey installer
	export DEBIAN_FRONTEND=noninteractive

	echo "== packages"
	apt-get update
	apt-get install -y ca-certificates curl git openssl caddy ufw python3-venv uidmap dbus-user-session slirp4netns libatomic1
	# Replace only the Ubuntu default Caddyfile or the dog Caddyfile; stop before any change to another one.
	if ! grep -qF "$DOG_CADDYFILE_HEADER" "$caddyfile" &&
		[[ $(sha256sum <"$caddyfile" | cut -d ' ' -f 1) != "$UBUNTU_CADDYFILE_SHA256" ]]; then
		echo "deploy-hub.sh: $caddyfile is not the Ubuntu default and not the dog Caddyfile; merge it by hand" >&2
		exit 1
	fi
	# Docker comes from the Docker apt repo: the Ubuntu docker.io package has no rootless setup tool.
	if [[ ! -e /etc/apt/sources.list.d/docker.sources ]]; then
		install -m 0755 -d /etc/apt/keyrings
		curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
		chmod a+r /etc/apt/keyrings/docker.asc
		# shellcheck source=/dev/null
		codename=$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
		cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $codename
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
		apt-get update
	fi
	apt-get install -y docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras
	# The root daemon stays off: a docker group member has root access, and root Docker bypasses ufw.
	systemctl disable --now docker.service docker.socket
	rm -f /var/run/docker.sock

	echo "== users"
	id -u doghub >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/doghub --shell /usr/sbin/nologin doghub
	id -u hermes >/dev/null 2>&1 || useradd --user-group --create-home --shell /bin/bash hermes

	echo "== firewall: 22, 80 and 443"
	if ! ufw status | grep -q '^Status: active'; then # an active ufw keeps its default policies
		ufw default deny incoming
		ufw default allow outgoing
	fi
	ufw allow 22/tcp
	ufw allow 80/tcp
	ufw allow 443/tcp
	ufw --force enable

	echo "== user manager and rootless Docker of hermes"
	loginctl enable-linger hermes
	uid=$(id -u hermes)
	systemctl start "user@$uid.service"
	if [[ ! -e /home/hermes/.config/systemd/user/docker.service ]]; then
		as_hermes "$uid" dockerd-rootless-setuptool.sh install
	fi
	as_hermes "$uid" systemctl --user enable --now docker.service
	as_hermes "$uid" docker context use rootless

	echo "== hub secrets and Hermes files (only when absent)"
	# The secrets go to sed through a here-doc, so they never appear in a process argument list.
	install -d -m 0755 /etc/doghub
	if [[ ! -e $hubenv ]]; then
		# An assignment, unlike a here-doc, stops the script when openssl fails, so no token is empty.
		device=$(openssl rand -hex 32)
		tools=$(openssl rand -hex 32)
		apikey=$(openssl rand -hex 32)
		(
			umask 077
			sed -f /dev/stdin "$src/deploy/hub/hub.env.example" >"$hubenv" <<EOF
s|^DEVICE_TOKEN=.*|DEVICE_TOKEN=$device|
s|^TOOLS_TOKEN=.*|TOOLS_TOKEN=$tools|
s|^HERMES_API_KEY=.*|HERMES_API_KEY=$apikey|
EOF
		)
	fi
	# Hermes seeds its own SOUL.md and config.yaml and never overwrites them, so the dog files go in first.
	install -d -o hermes -g hermes -m 0700 "$hh"
	if [[ ! -e $hh/.env ]]; then
		tools=$(env_value TOOLS_TOKEN "$hubenv")
		apikey=$(env_value HERMES_API_KEY "$hubenv")
		if [[ -z $tools || -z $apikey ]]; then
			echo "deploy-hub.sh: TOOLS_TOKEN or HERMES_API_KEY in $hubenv is empty; nothing was written" >&2
			exit 1
		fi
		(
			umask 077
			sed -f /dev/stdin "$src/deploy/hub/hermes.env.example" >"$hh/.env" <<EOF
s|^API_SERVER_KEY=.*|API_SERVER_KEY=$apikey|
s|^TOOLS_TOKEN=.*|TOOLS_TOKEN=$tools|
EOF
		)
		chown hermes:hermes "$hh/.env"
	fi
	[[ -e $hh/config.yaml ]] || install -o hermes -g hermes -m 0600 "$src/deploy/hub/hermes-config.yaml" "$hh/config.yaml"
	[[ -e $hh/SOUL.md ]] || install -o hermes -g hermes -m 0644 "$src/deploy/hub/SOUL.md" "$hh/SOUL.md"

	echo "== Hermes Agent $HERMES_TAG (the official installer of that tag, as hermes)"
	if [[ ! -x /home/hermes/.local/bin/hermes ]]; then
		installer=/home/hermes/hermes-install-$HERMES_TAG.sh
		as_hermes "$uid" curl -fsSL -o "$installer" \
			"https://raw.githubusercontent.com/NousResearch/hermes-agent/$HERMES_TAG/scripts/install.sh"
		echo "$HERMES_INSTALLER_SHA256  $installer" | sha256sum --check --quiet
		as_hermes "$uid" bash "$installer" --non-interactive --commit "$HERMES_COMMIT"
		rm -f "$installer"
	fi
	as_hermes "$uid" hermes gateway install --if-missing

	echo "== hub code in /opt/doghub (owner root, so the hub cannot change it)"
	install -d -m 0755 /opt/doghub
	rm -rf /opt/doghub/hub
	cp -r "$src/hub" /opt/doghub/hub
	[[ -x /opt/doghub/.venv/bin/python ]] || python3 -m venv /opt/doghub/.venv
	/opt/doghub/.venv/bin/python -m pip install --disable-pip-version-check -r /opt/doghub/hub/requirements.txt

	echo "== services"
	install -m 0644 "$src/deploy/hub/doghub.service" /etc/systemd/system/doghub.service
	if ! grep -qF "$DOG_CADDYFILE_HEADER" "$caddyfile"; then
		cp -p "$caddyfile" "$caddyfile.ubuntu-default"
	fi
	install -m 0644 "$src/deploy/hub/Caddyfile" "$caddyfile"
	install -d -m 0755 /etc/systemd/system/caddy.service.d
	printf '[Service]\nEnvironment=DOG_DOMAIN=%s\n' "$domain" >/etc/systemd/system/caddy.service.d/dog.conf
	systemctl daemon-reload
	systemctl enable doghub.service caddy.service
	systemctl restart caddy.service
	# The hub exits when OPENAI_API_KEY is empty, so it starts only after the owner adds the key.
	if [[ -n $(env_value OPENAI_API_KEY "$hubenv") ]]; then
		systemctl restart doghub.service
	else
		echo "doghub.service is enabled but not started: OPENAI_API_KEY in $hubenv is empty"
	fi
	rm -rf "$src"
	echo "Provisioning is complete. Do the manual steps in docs/runbook.md, sections 1.4 to 1.6."
}

# as_hermes runs a command as user hermes in its home, with the user manager and bus of hermes.
as_hermes() {
	local uid=$1
	shift
	(cd /home/hermes && runuser -u hermes -- env XDG_RUNTIME_DIR="/run/user/$uid" \
		DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
		PATH="/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@")
}

# env_value prints the value of one KEY=value line of an env file.
env_value() {
	sed -n "s/^$1=//p" "$2"
}

if [[ ${1:-} == --remote ]]; then
	remote "$2" "$3"
	exit 0
fi

# The domain goes into a remote command line, so it may hold only host name characters.
if [[ $# -ne 2 || ! $2 =~ ^[A-Za-z0-9.-]+$ ]]; then
	echo "usage: $0 <ssh-target> <domain>   (example: $0 root@203.0.113.10 example.com)" >&2
	exit 3
fi
target=$1
domain=$2
cd "$(dirname "$0")/.."
src=$(ssh "$target" mktemp -d)
COPYFILE_DISABLE=1 tar --exclude __pycache__ -czf - hub deploy/hub scripts/deploy-hub.sh |
	ssh "$target" tar -xzf - -C "$src"
# shellcheck disable=SC2029 # $src and $domain expand on the laptop on purpose.
ssh -t "$target" sudo bash "$src/scripts/deploy-hub.sh" --remote "$domain" "$src"
