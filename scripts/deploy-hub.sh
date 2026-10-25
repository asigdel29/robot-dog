#!/usr/bin/env bash
# deploy-hub.sh: provisions the droplet (Caddy, ufw, the hub, rootless Docker and Hermes Agent) over SSH.
# Usage from the laptop: scripts/deploy-hub.sh <ssh-target> <domain>
#   <ssh-target> is root@<host> or a sudo user; <domain> gives the site dog.<domain>.
# It copies hub/ and deploy/hub/ to the host and runs this script there with --remote as root.
# Each step is idempotent. Secrets and config files are created only when they are absent.
set -euo pipefail

usage() {
	echo "usage: $0 <ssh-target> <domain>   (example: $0 root@203.0.113.10 example.com)" >&2
	exit 3
}

# remote runs on the droplet as root; $1 is the domain and $2 is the copied source directory.
remote() {
	local domain=$1 src=$2
	local hh=/home/hermes/.hermes hubenv=/etc/doghub/hub.env uid codename
	[[ $EUID -eq 0 ]] || {
		echo "deploy-hub.sh --remote: run it as root" >&2
		exit 3
	}
	export DEBIAN_FRONTEND=noninteractive

	echo "== packages"
	apt-get update
	apt-get install -y ca-certificates curl git openssl caddy ufw python3-venv uidmap dbus-user-session slirp4netns libatomic1
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
	id -u doghub >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/doghub --no-create-home --shell /usr/sbin/nologin doghub
	id -u hermes >/dev/null 2>&1 || useradd --user-group --create-home --shell /bin/bash hermes
	grep -q '^hermes:' /etc/subuid || {
		echo "hermes has no /etc/subuid range; rootless Docker needs one" >&2
		exit 1
	}

	echo "== firewall: 22, 80 and 443 only"
	ufw default deny incoming
	ufw default allow outgoing
	ufw allow 22/tcp
	ufw allow 80/tcp
	ufw allow 443/tcp
	ufw --force enable

	echo "== user manager of hermes (linger)"
	loginctl enable-linger hermes
	uid=$(id -u hermes)
	systemctl start "user@$uid.service"

	echo "== rootless Docker for hermes"
	if [[ ! -e /home/hermes/.config/systemd/user/docker.service ]]; then
		as_hermes "$uid" dockerd-rootless-setuptool.sh install
	fi
	as_hermes "$uid" systemctl --user enable --now docker.service
	as_hermes "$uid" docker context use rootless

	echo "== hub secrets and Hermes files (only when absent)"
	install -d -m 0755 /etc/doghub
	if [[ ! -e $hubenv ]]; then
		(
			umask 077
			sed -e "s|^DEVICE_TOKEN=.*|DEVICE_TOKEN=$(token)|" \
				-e "s|^TOOLS_TOKEN=.*|TOOLS_TOKEN=$(token)|" \
				-e "s|^HERMES_API_KEY=.*|HERMES_API_KEY=$(token)|" \
				"$src/deploy/hub/hub.env.example" >"$hubenv"
		)
	fi
	# Hermes seeds its own SOUL.md and config.yaml and never overwrites them, so the dog files go in first.
	install -d -o hermes -g hermes -m 0700 "$hh"
	if [[ ! -e $hh/.env ]]; then
		(
			umask 077
			sed -e "s|^API_SERVER_KEY=.*|API_SERVER_KEY=$(env_value HERMES_API_KEY "$hubenv")|" \
				-e "s|^TOOLS_TOKEN=.*|TOOLS_TOKEN=$(env_value TOOLS_TOKEN "$hubenv")|" \
				"$src/deploy/hub/hermes.env.example" >"$hh/.env"
		)
		chown hermes:hermes "$hh/.env"
	fi
	seed "$src/deploy/hub/hermes-config.yaml" "$hh/config.yaml" 0600
	seed "$src/deploy/hub/SOUL.md" "$hh/SOUL.md" 0644

	echo "== Hermes Agent (official installer, as hermes)"
	if [[ ! -x /home/hermes/.local/bin/hermes ]]; then
		as_hermes "$uid" bash -c 'set -euo pipefail; curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --non-interactive'
	fi
	as_hermes "$uid" hermes gateway install --if-missing

	echo "== hub release in /opt/doghub"
	install -d -m 0755 /opt/doghub
	rm -rf /opt/doghub/hub.new
	cp -r "$src/hub" /opt/doghub/hub.new
	chown -R root:root /opt/doghub/hub.new
	rm -rf /opt/doghub/hub
	mv /opt/doghub/hub.new /opt/doghub/hub
	[[ -x /opt/doghub/.venv/bin/python ]] || python3 -m venv /opt/doghub/.venv
	/opt/doghub/.venv/bin/python -m pip install --disable-pip-version-check -r /opt/doghub/hub/requirements.txt

	echo "== services"
	install -m 0644 "$src/deploy/hub/doghub.service" /etc/systemd/system/doghub.service
	install -m 0644 "$src/deploy/hub/Caddyfile" /etc/caddy/Caddyfile
	install -d -m 0755 /etc/systemd/system/caddy.service.d
	printf '[Service]\nEnvironment=DOG_DOMAIN=%s\n' "$domain" >/etc/systemd/system/caddy.service.d/dog.conf
	systemctl daemon-reload
	systemctl enable doghub.service caddy.service
	systemctl restart doghub.service caddy.service

	cat <<EOF

Provisioning is complete. Do the manual steps in docs/runbook.md, section "Droplet bring-up":
  1. Put OPENROUTER_API_KEY, VOICE_TOOLS_OPENAI_KEY and TELEGRAM_BOT_TOKEN (from BotFather) in $hh/.env.
  2. Put OPENAI_API_KEY in $hubenv, then run: systemctl restart doghub
  3. Put the vision model id in $hh/config.yaml (model.default).
  4. Run: sudo -iu hermes systemctl --user restart hermes-gateway
  5. Send a DM to the bot. Then run: sudo -iu hermes hermes pairing approve telegram <code>
  6. Copy DEVICE_TOKEN from $hubenv into /etc/dog/link.env on the Pi.
EOF
}

# as_hermes runs a command as user hermes in its home, with the user manager and bus of hermes.
as_hermes() {
	local uid=$1
	shift
	(cd /home/hermes && runuser -u hermes -- env XDG_RUNTIME_DIR="/run/user/$uid" \
		DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
		PATH="/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@")
}

# token prints a new random secret of 32 bytes in hex.
token() {
	openssl rand -hex 32
}

# env_value prints the value of one KEY=value line of an env file.
env_value() {
	sed -n "s/^$1=//p" "$2"
}

# seed copies a template for user hermes only when the target is absent.
seed() {
	local src=$1 dst=$2 mode=$3
	[[ -e $dst ]] || install -o hermes -g hermes -m "$mode" "$src" "$dst"
}

if [[ ${1:-} == --remote ]]; then
	[[ $# -eq 3 ]] || usage
	remote "$2" "$3"
	exit 0
fi

[[ $# -eq 2 ]] || usage
target=$1
domain=$2
[[ $domain =~ ^[A-Za-z0-9.-]+$ ]] || usage
cd "$(dirname "$0")/.."
[[ -f hub/hub.py && -f hub/requirements.txt ]] || {
	echo "deploy-hub.sh: hub/hub.py and hub/requirements.txt are necessary" >&2
	exit 3
}
src=$(ssh "$target" mktemp -d)
COPYFILE_DISABLE=1 tar --exclude __pycache__ -czf - hub deploy/hub scripts/deploy-hub.sh |
	ssh "$target" tar -xzf - -C "$src"
# shellcheck disable=SC2029 # $src and $domain expand on the laptop on purpose.
ssh -t "$target" sudo bash "$src/scripts/deploy-hub.sh" --remote "$domain" "$src"
