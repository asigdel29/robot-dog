# robot-dog

This repo gives a voice and an agent to a SUPIREO Y8359 toy robot dog. The toy has only an infrared (IR) remote. A Raspberry Pi and an ESP32 IR bridge send the same IR frames as the remote. A Hermes Agent on a server listens, talks, does web tasks and starts verified tricks.

The software architecture follows [microduck](https://github.com/pollen-robotics/microduck) by Pollen Robotics (Apache-2.0): one daemon for each concern, JSON-RPC over unix sockets, one writer for each state, a deadman, start-up that never moves, and health-gated releases. This repo copies no microduck code.

## Parts

| Part | Where | Job |
| --- | --- | --- |
| `pi/robotd.py` | Pi | Only process that talks to the IR bridge. 20 Hz loop, registry, arm state, actions, safety. |
| `pi/robotctl.py` | Pi | Command-line control, the only writer of the registry, and the release update. |
| `pi/dogproto.py` | Pi | The local bus: JSON-RPC 2.0, one object per line, two sockets for each daemon. |
| `firmware/ir_bridge/` | ESP32 Feather V2 | IR capture and replay, leases, heartbeat watchdog. |
| `sim/fake_esp32.py` | Laptop, CI | The bridge protocol on a pty, with a simulated toy. |
| `sim/devstack.py` | Laptop, CI | Starts the stack against the fake bridge. |

The design documents in `docs/design/` own the mechanisms:

- `protocol.md`: the wire formats.
- `safety.md`: arm rules, limits, stop semantics, watchdogs.
- `registry.md`: the trick registry and the capture procedure.
- `release.md`: the release file, the update, the health gate and the rollback.

## Deploy

`docs/runbook.md` gives the full procedures for the droplet and the Pi.

| Command | Where | Job |
| --- | --- | --- |
| `scripts/package.sh` | Laptop | Makes `dist/dog-<version>.tar.gz` and its `.sha256` file. |
| `scripts/install-pi.sh` | Pi, one time | Installs the apt packages and applies the first release. |
| `scripts/deploy-pi.sh` | Laptop | Makes a release, copies it to `robot-pi` and applies it with the health gate. |
| `robotctl update status`, `robotctl update rollback` | Pi | Shows the releases, or makes the previous release current again. |
| `scripts/deploy-hub.sh <ssh-target> <domain>` | Laptop | Installs Caddy, ufw, the hub, rootless Docker and Hermes Agent on the droplet. |

The units and templates are in `deploy/pi/` and `deploy/hub/`. The release hooks are in `hooks/`.

## Run the tests

1. Install Python 3.11 or later.
2. Run `python -m unittest discover -v -s tests` from the repo root.

The tests need no hardware. `tests/test_conformance.py` runs against the real bridge when `DOG_SERIAL` names its port:

~~~bash
DOG_SERIAL=/dev/cu.usbserial-XXXX python -m unittest -v tests.test_conformance
~~~

## Run the dev stack

1. Run `python sim/devstack.py`. It prints a run directory.
2. In a second terminal, set `DOG_RUN_DIR` to that directory.
3. Run `python pi/robotctl.py --config /dev/null state`.
4. Run `python pi/robotctl.py --config /dev/null enable`.
5. Run `python pi/robotctl.py --config /dev/null do bark`.

## Safety

- Only a person at the Pi can arm the dog. The cloud can never arm it.
- An IR stop is not a dependable emergency stop. Keep the toy power switch in reach.
- `transmitted: true` only means that IR frames left the bridge. The camera result is `observed`.
