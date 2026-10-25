# Runbook

This runbook gives the procedures for phase 4 (the droplet) and phase 6 (the Pi). It names two persons. The owner holds the accounts and the hardware. The implementer does all other work.

- A step with **(Owner)** is for the owner only. The implementer cannot do it.
- All other steps are for the implementer.
- Each secret goes from the owner to the implementer through the password manager. Never send a secret by email or chat.

`docs/design/release.md` owns the release mechanism. This runbook tells you only how to use it.

Merge the phase 4 branch after the phase 3 branches. The mediad, padd and linkd units start daemons that only the phase 3 branches contain. Before that merge, those three units restart in a loop on the Pi.

## 1. Phase 4: install the droplet

### 1.1 Accounts and the droplet (Owner)

1. **(Owner)** Create a DigitalOcean droplet: Ubuntu 24.04, 2 vCPU, 4 GB of memory.
2. **(Owner)** Add the SSH public key of the implementer to the droplet when you create it.
3. **(Owner)** Create a DNS A record. The name is `dog.<domain>` and the value is the droplet IP address.
4. **(Owner)** Create an OpenRouter API key with a credit limit.
5. **(Owner)** In the OpenRouter privacy values, select only providers that do not train on your data.
6. **(Owner)** Create an OpenAI project with a monthly budget.
7. **(Owner)** Create an OpenAI API key in that project.
8. **(Owner)** In Telegram, send `/newbot` to BotFather. BotFather replies with a bot token.
9. **(Owner)** Put the OpenRouter key, the OpenAI key and the bot token in the password manager.
10. **(Owner)** Share these three items with the implementer.

The OpenRouter credit limit is the only hard cost limit. The OpenAI budget and the hub daily caps are soft limits.

### 1.2 Install the droplet software

`scripts/deploy-hub.sh` installs Caddy, ufw, the hub, rootless Docker and Hermes Agent. It is idempotent: a second run changes only the code and the units. It never replaces a file that holds secrets or owner values.

1. Make sure that `dig +short dog.<domain>` gives the droplet IP address.
2. On the laptop, run `scripts/deploy-hub.sh root@<droplet-ip> <domain>`.
3. When the script completes, do sections 1.4 to 1.6.

The script does these steps on the droplet:

- It installs Caddy, ufw, the packages for rootless Docker, and Docker from the Docker apt repository.
- It stops when `/etc/caddy/Caddyfile` is not the Ubuntu default and not an earlier dog Caddyfile. This check occurs before all other changes.
- It stops and disables the root Docker daemon. A member of the `docker` group has root access, and root Docker bypasses ufw.
- It creates the users `doghub` (system user, no login) and `hermes` (normal user, no password).
- It opens ports 22, 80 and 443 in ufw. When ufw is already active, the script keeps its default policies.
- It runs `loginctl enable-linger hermes` and starts rootless Docker for `hermes`.
- It creates `/etc/doghub/hub.env` and `~hermes/.hermes/.env` with new random values for `DEVICE_TOKEN`, `TOOLS_TOKEN` and the API server key.
- It never writes an empty token. A secret never appears in a process argument list.
- It copies `hermes-config.yaml` and `SOUL.md` into `~hermes/.hermes/` before the Hermes installer runs. Hermes keeps a `SOUL.md` that is already there.
- It downloads `scripts/install.sh` of the pinned Hermes release tag and checks its sha256. It runs that file as `hermes` with `--non-interactive` and the pinned commit.
- It installs the Hermes gateway as a user service.
- It copies `hub/` to `/opt/doghub/hub/` as root, makes `/opt/doghub/.venv` and installs `hub/requirements.txt`.
- It installs `doghub.service`, the Caddyfile and a Caddy drop-in that sets `DOG_DOMAIN`. It keeps the Ubuntu default as `/etc/caddy/Caddyfile.ubuntu-default`.
- It starts `doghub` only when `OPENAI_API_KEY` in `hub.env` has a value.

### 1.3 Update the Hermes pin

`scripts/deploy-hub.sh` installs one pinned Hermes release. Three values at the top of the script give the pin. Change them together:

1. Find the new release tag on the NousResearch/hermes-agent releases page, for example `v0.23.0`.
2. Run `curl -fsSL -o install.sh https://raw.githubusercontent.com/NousResearch/hermes-agent/<tag>/scripts/install.sh`.
3. Run `sha256sum install.sh`.
4. Examine the new flags and the new downloads in `install.sh` before you trust it.
5. Run `gh api repos/NousResearch/hermes-agent/commits/<tag> --jq .sha` to get the commit of the tag.
6. Write the tag in `HERMES_TAG`.
7. Write the hash in `HERMES_INSTALLER_SHA256`.
8. Write the commit in `HERMES_COMMIT`.
9. Make a commit of the change.

The pin applies to a new install only. On a droplet that already has Hermes, run `sudo -iu hermes hermes update`. Then do the phase 4 checks again.

### 1.4 Add the keys

Do these steps on the droplet. `sudoedit` keeps the file owner and mode.

1. Open the Hermes env file: `sudoedit -u hermes /home/hermes/.hermes/.env`.
2. Write the OpenRouter key after `OPENROUTER_API_KEY=`.
3. Write the OpenAI key after `VOICE_TOOLS_OPENAI_KEY=`.
4. Write the bot token after `TELEGRAM_BOT_TOKEN=`.
5. Open the hub env file: `sudoedit /etc/doghub/hub.env`.
6. Write the OpenAI key after `OPENAI_API_KEY=`.
7. Open the Hermes configuration: `sudoedit -u hermes /home/hermes/.hermes/config.yaml`.
8. Replace `REPLACE-WITH-VISION-MODEL` with an OpenRouter model id that accepts images.
9. Run `sudo systemctl restart doghub`.
10. Run `sudo -iu hermes systemctl --user restart hermes-gateway`.

The `look` tool returns a JPEG image. A model that accepts only text cannot see that image.

### 1.5 Pair the owner in Telegram

1. **(Owner)** Send a direct message to the bot. The bot replies with a pair code.
2. **(Owner)** Send the pair code to the implementer.
3. Run `sudo -iu hermes hermes pairing approve telegram <code>`.
4. Run `sudo -iu hermes hermes pairing list`. The output shows the numeric user id of the owner.
5. In `~hermes/.hermes/.env`, write that id after `TELEGRAM_ALLOWED_USERS=`.
6. In `~hermes/.hermes/config.yaml`, change `unauthorized_dm_behavior: pair` to `unauthorized_dm_behavior: ignore`.
7. Run `sudo -iu hermes systemctl --user restart hermes-gateway`.

With `ignore`, Hermes gives no answer to a direct message from an account that is not on the allowlist.

### 1.6 Phase 4 checks

Do each check. Write each result in `docs/test-log.md`, with the date, the command and the measured value.

| Check | Command or action | Pass condition |
| --- | --- | --- |
| TLS | `curl -sS https://dog.<domain>/healthz` | A reply from the hub with a valid certificate. |
| Other paths are closed | `curl -s -o /dev/null -w '%{http_code}\n' https://dog.<domain>/tools/` | `404`. |
| Firewall | `sudo ufw status` | Only 22, 80 and 443 are open. |
| Loopback binds | `sudo ss -tlnp` | Ports 8000 and 8642 listen on 127.0.0.1 only. |
| Hermes health | `sudo -iu hermes hermes doctor` | Exit code 0. |
| Hermes values | `sudo -iu hermes hermes config check` | No error. |
| Tool server | `sudo -iu hermes hermes mcp test dog` | The list has 7 tools. |
| Allowlist | **(Owner)** Send a message to the bot from a second Telegram account. | No answer. |
| Motion path | Start the laptop devstack. Give its linkd `DOG_HUB_URL=wss://dog.<domain>/device` and the `DOG_DEVICE_TOKEN`. Send "Bark" in Telegram. | The fake bridge log on the laptop has a `send`. |
| Vision | Send "What do you see?" in Telegram. | Hermes describes the synthetic frame. |
| Terminal cannot read secrets | Ask Hermes to run `cat ~/.hermes/.env` in its terminal. | The command fails. The container does not have that file. |
| Terminal cannot reach the tools | Ask Hermes to run `curl -s -m 5 http://10.0.2.2:8000/tools/` in its terminal. | No connection. Rootless Docker does not give the container the host loopback. |
| Tools need the token | On the droplet, run `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/tools/`. | `401`. |
| Root Docker is off | `systemctl is-active docker.service` | `inactive`. |

### 1.7 Privacy and cost after phase 4

- Hermes keeps transcripts and tool images in `~hermes/.hermes/`. Delete old files on a schedule.
- The hub keeps no audio and no images. It keeps only the daily totals in `/var/lib/doghub/usage.json`.
- Configure no Hermes cron jobs unless the owner asks for one.

## 2. Phase 6: install the Pi

### 2.1 Write the SD card (Owner)

1. **(Owner)** In Raspberry Pi Imager, select Raspberry Pi 5.
2. **(Owner)** Select Raspberry Pi OS Lite (64-bit), the release based on Debian 13 Trixie.
3. **(Owner)** In the OS customization, set the hostname to `robot-pi`.
4. **(Owner)** Set the user name to `robot`.
5. **(Owner)** Set a strong password.
6. **(Owner)** Keep the password in the password manager.
7. **(Owner)** Enter the Wi-Fi name, the Wi-Fi password and the Wi-Fi country.
8. **(Owner)** Set the time zone and the keyboard layout.
9. **(Owner)** On the Services tab, select SSH with public-key authentication only.
10. **(Owner)** Paste the SSH public key of the implementer.
11. **(Owner)** Write the card.
12. **(Owner)** Put the card in the Pi.
13. **(Owner)** Connect the power supply.

The Bookworm release of Raspberry Pi OS is not correct for this project. Its OpenCV is 4.6, and mediad needs `cv2.aruco.ArucoDetector` from OpenCV 4.7 or later. `preinstall` refuses a system without it.

### 2.2 First install

1. On the laptop, add a `Host robot-pi` entry with `User robot` to `~/.ssh/config`.
2. Run `ssh robot-pi true`. It must complete with no password prompt.
3. Run `scripts/package.sh`. The script prints the tarball path, for example `dist/dog-v0.4.0.tar.gz`.
4. Run `scp dist/dog-<version>.tar.gz dist/dog-<version>.tar.gz.sha256 scripts/install-pi.sh robot-pi:`.
5. Run `ssh -t robot-pi sudo bash install-pi.sh dog-<version>.tar.gz`.
6. Make sure that the output shows `"health"` with the value `"degraded"` or `"ok"`.

`install-pi.sh` installs the apt packages. Then it runs `robotctl update apply` from the tarball. The hooks create the users, the directories, the units and the `/etc/dog` files. The health gate commits approximately 10 s after the restart.

When the first install fails, no release is current, and `dog.target` is stopped and disabled. Correct the fault. Then do step 5 again with the same tarball.

### 2.3 Configure the Pi

1. Run `ls -l /dev/serial/by-id/` on the Pi. The output shows the path of the Feather.
2. Run `sudoedit /etc/dog/dog.toml`.
3. Write the Feather path after `serial =`.
4. Run `sudoedit /etc/dog/link.env`.
5. Write `wss://dog.<domain>/device` after `DOG_HUB_URL=`.
6. In the same file, write the droplet value of `DEVICE_TOKEN` after `DOG_DEVICE_TOKEN=`.
7. Run `sudoedit /etc/dog/console.env`.
8. Write the output of `openssl rand -hex 24` after `DOG_CONSOLE_TOKEN=`.
9. Run `sudo systemctl restart dog.target`.

The droplet value of `DEVICE_TOKEN` is in `/etc/doghub/hub.env`. The console token goes in a browser cookie, so it must be hex.

The LAN console of mediad is plain HTTP on `console_port` in the `[mediad]` section of `dog.toml` (8080 by default). Use it only on the home LAN. Do not forward that port on the router.

### 2.4 Phase 6 checks

| Check | Command | Pass condition |
| --- | --- | --- |
| Units after a reboot | `sudo reboot`, then `systemctl is-active robotd mediad padd linkd` | Each line is `active`. |
| Start never arms | `robotctl state` | `"armed": false`. |
| Bridge access through the device policy | Connect the Feather, then run `robotctl state`. | `"esp"` shows a board state, not a link failure. |
| Releases | `robotctl update status` | `current` is the new version. |
| Rollback time | The procedure in section 2.5. | The old release is current again in 40 s or less. |

### 2.5 Test the rollback

1. Make a temporary branch: `git switch -c rollback-test`.
2. Add `raise SystemExit("rollback test")` below the header comment of `pi/robotd.py`.
3. Make a commit of that change on the temporary branch.
4. Run `time scripts/deploy-pi.sh`.
5. Make sure that the output shows `rolled back to <old version>`.
6. Make sure that the time is 40 s or less.
7. Run `ssh robot-pi robotctl update status`.
8. Make sure that `current` is the old version.
9. Return to your branch with `git switch -`.
10. Delete the temporary branch: `git branch -D rollback-test`.

### 2.6 Later releases

1. Make a commit of each change. A file that git does not track is not in the release.
2. Run `scripts/deploy-pi.sh`.
3. To make the previous release current again, run `ssh robot-pi sudo robotctl update rollback`.

When the SSH connection stops during an update, the update continues on the Pi. To see the result, connect again and run `robotctl update status`.

### 2.7 Logs

| Item | Command |
| --- | --- |
| A daemon on the Pi | `journalctl -u robotd -b` (or `mediad`, `padd`, `linkd`) |
| The hub | `journalctl -u doghub -b` |
| The result of each update on the Pi | `journalctl -t robotctl` |
| Caddy | `journalctl -u caddy -b` |
| The Hermes gateway | `sudo -iu hermes journalctl --user -u hermes-gateway -b` |

## 3. Hermes values that the docs do not confirm

The Hermes docs were read on 2026-10-25 (`https://hermes-agent.nousresearch.com/docs/`). They confirm each key in `deploy/hub/hermes-config.yaml` and `deploy/hub/hermes.env.example`. They do not confirm the items in this table. Check each item on the droplet before you rely on it.

| Item | Where it is used | Status and check |
| --- | --- | --- |
| Rootless Docker as the terminal backend | `terminal.backend: docker` with the `rootless` CLI context of `hermes` | The docs do not describe rootless Docker. Make sure that `sudo -iu hermes docker info` shows `rootless`. Do the terminal checks in section 1.6. |
| `install.sh --non-interactive --commit <sha>` | `deploy-hub.sh` | The docs give `--non-interactive`. `--commit` is in the usage text of `install.sh`, not in the docs. Make sure that `~hermes/.local/bin/hermes` exists after the script. |
| `hermes gateway install --if-missing` | `deploy-hub.sh` | The flag is in `install.sh`, not in the CLI reference. Run `sudo -iu hermes systemctl --user status hermes-gateway`. |
| Default of `agent.max_turns` | `hermes-config.yaml` sets 40 | The docs give two defaults (unlimited and 500). The template sets an explicit value, so the default does not apply. |
| Default of the tool server `timeout` | `hermes-config.yaml` sets 60 | The docs give 300 in a table and 120 in an example. The template sets an explicit value. |
| `gateway.api_server` keys in `config.yaml` | Not used | The API server page contradicts itself. The template uses the `API_SERVER_*` environment values in `.env`, which the docs confirm. |
| OpenAI text-to-speech with `OPENAI_API_KEY` | Not used | The docs confirm this fallback only for speech-to-text. The template uses `VOICE_TOOLS_OPENAI_KEY`. |
| Default sandbox image | Not set | The docs give two different images. Run `sudo -iu hermes hermes config get terminal.docker_image`. |
| A switch that disables cron | Not used | The docs have no such switch. Cron runs only the jobs that you create, and the template creates none. |
| The unattended-mode key | `approvals.unattended_mode: deny` | The docs confirm the key. The plan says to check it with `hermes config` before you rely on it. Run `sudo -iu hermes hermes config get approvals.unattended_mode`. |
