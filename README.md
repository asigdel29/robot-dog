# robot-dog

[![test](https://github.com/asigdel29/robot-dog/actions/workflows/test.yml/badge.svg)](https://github.com/asigdel29/robot-dog/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

This project gives a voice and an agent to a toy robot dog. A small board sends the same infrared (IR) commands as the toy remote. A Hermes Agent on a server listens, talks, does web tasks and starts verified tricks.

## Contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Hardware](#hardware)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Usage](#usage)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Tests](#tests)
- [Project structure](#project-structure)
- [Safety](#safety)
- [Documentation](#documentation)
- [Contributions](#contributions)
- [Security](#security)
- [License](#license)
- [Acknowledgements](#acknowledgements)

## Overview

The toy is a SUPIREO Y8359 robot dog. It has an IR remote with 19 buttons and small wheels under its feet. It has no servos and no interface for its joints. It has no Wi-Fi and no Bluetooth, and its USB-C port only charges it. Thus this project controls the toy at the level of its remote buttons.

The software architecture follows [microduck](https://github.com/pollen-robotics/microduck) by Pollen Robotics:

- One daemon for each concern.
- JSON-RPC over unix sockets.
- One writer for each state.
- A deadman on continuous motion.
- A start-up that never moves the dog.
- Releases with a health gate.

This repo copies no microduck code.

Status: the Pi path works against a simulated IR bridge and a simulated toy. The current hardware plan is the Atom Echo path in `docs/design/echo.md`. Its firmware is not written yet (#143). The GitHub issues give the full plan and its order.

## Features

- Voice: hold a gamepad button, speak, and hear the reply from the dog speaker.
- Remote chat: send text or voice notes to the dog through Telegram.
- Web tasks: Hermes Agent uses its own tools for search and other internet tasks.
- Tricks and motion: Hermes starts only verified tricks and bounded moves.
- Navigation: the dog drives to a printed ArUco marker with camera feedback.
- Local control: a wired gamepad and a LAN web page arm, stop and drive the dog.
- Safe updates: a release that does not become healthy rolls back by itself.

## Architecture

~~~mermaid
flowchart LR
  subgraph Pi["Raspberry Pi 5"]
    PADD["padd: gamepad"]
    MEDIAD["mediad: camera, markers, audio, LAN console"]
    ROBOTD["robotd: 20 Hz loop, only serial writer"]
    LINKD["linkd: only process with internet access"]
  end
  ESP["ESP32 IR bridge"]
  DOG["Toy dog"]
  subgraph Server["Droplet"]
    HUB["hub: device link, robot tools, voice loop"]
    HERMES["Hermes Agent: API server, Telegram"]
  end
  PADD --> ROBOTD
  MEDIAD --> ROBOTD
  LINKD --> ROBOTD
  LINKD --> MEDIAD
  ROBOTD -->|USB serial| ESP
  ESP -->|IR| DOG
  LINKD <-->|WSS| HUB
  HUB <-->|loopback| HERMES
~~~

| Process | Host | Job |
| --- | --- | --- |
| `pi/robotd.py` | Pi | Owns the IR bridge link, the trick registry, the arm state, one action at a time and the safety rules. |
| `pi/mediad.py` | Pi | Owns the camera, the marker pose, the microphone, the speaker and the LAN console. |
| `pi/padd.py` | Pi | Turns gamepad input into arm, stop, drive and push-to-talk. |
| `pi/linkd.py` | Pi | Keeps one outbound WSS link to the hub. It cannot arm the dog. |
| `pi/robotctl.py` | Pi | Command-line control, registry capture and release updates. |
| `firmware/ir_bridge/` | ESP32 | Records and sends IR frames, with leases and a heartbeat watchdog. |
| `hub/hub.py` | Droplet | Holds the Pi link, gives seven robot tools to Hermes, and runs the voice loop. |

## Hardware

### Atom Echo path (now)

| Part | Job | Buy |
| --- | --- | --- |
| SUPIREO Y8359 robot dog and its remote | The robot. The remote is necessary to record the IR codes. | Owned |
| M5Stack Atom Echo (SKU C008-C) | Wi-Fi, microphone, speaker, button. Runs the action engine. | [Amazon](https://www.amazon.com/dp/B0C7QSVPB2), [Hackster](https://hackster.io/products/buy/74003) |
| M5Stack Unit IR (SKU U002), with a Grove cable | IR emitter and IR receiver. | [Amazon](https://www.amazon.com/dp/B08TCC28VZ), [Hackster](https://hackster.io/products/buy/71896) |
| USB-C data cable and a 5 V USB charger | Flash and power for the board. | Owned |

`docs/design/echo.md` gives the pins, the setup and the plan.

### Pi path (camera phase, later)

| Part | Job |
| --- | --- |
| Raspberry Pi 5 (2 GB), 27 W power supply, case with fan, 32 GB microSD | Runs the Pi daemons. |
| Adafruit ESP32 Feather V2 (#5900) | IR bridge. |
| Adafruit IR transceiver (#5990) and JST PH 4-pin cable (#3950) | IR receiver and emitter. |
| USB-A to USB-C data cable | Connects the Feather to the Pi. |
| Logitech C270 webcam and a stand | Camera for the marker pose. |
| Wired USB gamepad (Logitech F310 class) | Local arm, stop, drive and push-to-talk. |
| USB speaker | Voice output. The Pi 5 has no audio jack. |
| Printed ArUco markers (DICT_4X4_50) | One marker on the dog, one marker for each goal. |

Connect the IR module to the Feather as `firmware/README.md` shows. Check each connection with the labels on the pads, not with the wire colors.

## Requirements

| Where | Requirement |
| --- | --- |
| Development computer | macOS or Linux, Python 3.11 or later, [uv](https://docs.astral.sh/uv/) or pip |
| Firmware | arduino-cli 1.5.1, esp32 core 3.3.12, IRremote 4.7.1, ArduinoJson 7.4.3 |
| Pi | Raspberry Pi OS Lite 64-bit on Debian 13 Trixie (Python 3.13, OpenCV 4.10) |
| Server | Ubuntu 24.04 with 2 vCPU and 4 GB RAM, a DNS name, OpenRouter and OpenAI keys, a Telegram bot token |

Do not use Raspberry Pi OS on Debian 12 Bookworm. Its OpenCV 4.6 has no `ArucoDetector`.

## Quick start

This procedure runs the full Pi stack on a development computer. A simulated IR bridge and a simulated toy replace the hardware.

1. Clone the repo:

   ~~~bash
   git clone https://github.com/asigdel29/robot-dog.git
   cd robot-dog
   ~~~

2. Make a virtual environment and install the test packages:

   ~~~bash
   uv venv --python 3.11 .venv
   uv pip install --python .venv/bin/python -r requirements-dev.txt
   ~~~

3. Run the tests:

   ~~~bash
   .venv/bin/python -m unittest discover -s tests
   ~~~

4. Start the dev stack. It prints a run directory:

   ~~~bash
   .venv/bin/python sim/devstack.py
   ~~~

5. In a second terminal, set `DOG_RUN_DIR` to that run directory.
6. Arm the simulated dog:

   ~~~bash
   .venv/bin/python pi/robotctl.py --config /dev/null enable
   ~~~

7. Start a trick:

   ~~~bash
   .venv/bin/python pi/robotctl.py --config /dev/null do bark
   ~~~

## Usage

### Gamepad

| Control | Action |
| --- | --- |
| Start | Arm the dog. |
| B | Stop. |
| Hold Select for 1 s | Disarm the dog. |
| Left stick | Drive forward, back, left or right. |
| Hold A | Record speech for the agent (push-to-talk). |
| X, Y, LB, RB | Start the tricks in `[padd.bindings]` of `dog.toml`. |

### robotctl on the Pi

| Command | Action |
| --- | --- |
| `robotctl state` | Shows the arm state, the health, the tricks and the capabilities. |
| `robotctl enable`, `robotctl disarm`, `robotctl stop` | Arms, disarms or stops the dog. |
| `robotctl do <trick>` | Starts one verified trick. |
| `robotctl drive <dir> <ms>` | Drives in one direction for a bounded time. |
| `robotctl goto <marker>` | Drives to an ArUco marker. |
| `robotctl registry capture <name>` | Records one remote button. |
| `robotctl registry test <name>` | Sends one recorded button. The dog must be armed. |
| `robotctl registry verify <name> ...` | Records the tested behavior and marks the button as verified. |
| `robotctl update apply`, `rollback`, `status` | Installs a release, goes back to the previous release, or shows the releases. |

From the development computer, run each command over SSH, for example `ssh robot-pi robotctl state`.

### Voice and chat

1. Arm the dog with Start.
2. Hold A and speak. Release A to send the speech.
3. Listen to the reply from the dog speaker.

Telegram messages go to the same Hermes Agent. Motion from Telegram works only while a person at the Pi keeps the dog armed.

## Configuration

| File | Location on the Pi | Content |
| --- | --- | --- |
| `config/dog.toml` | `/etc/dog/dog.toml` | Device paths, ALSA devices, marker ids, console port, gamepad bindings. |
| `config/registry.json` | `/etc/dog/registry.json` | The recorded IR commands and their tested limits. Only `robotctl` writes it. |
| `deploy/pi/link.env.example` | `/etc/dog/link.env` | `DOG_HUB_URL`, `DOG_DEVICE_TOKEN`, `DOG_DEVICE`. |
| `deploy/pi/console.env.example` | `/etc/dog/console.env` | `DOG_CONSOLE_TOKEN` (32 characters or more). |
| `deploy/hub/hub.env.example` | `/etc/doghub/hub.env` on the server | Hub tokens, the Hermes URL and key, OpenAI key and models, daily caps. |
| `deploy/hub/hermes-config.yaml` | `~hermes/.hermes/config.yaml` on the server | Model, API server, robot tool server, Docker terminal, approvals, voice. |

Keep each env file out of git. Each one has mode 0600 or 0640.

## Deployment

`docs/runbook.md` gives the full procedures. The short form:

1. Create the droplet with your SSH key and a DNS record for `dog.<domain>`.
2. Run `scripts/deploy-hub.sh <ssh-target> <domain>` from the development computer.
3. Write Raspberry Pi OS Lite to the SD card. Use hostname `robot-pi` and user `robot`.
4. Run `scripts/install-pi.sh` on the Pi one time.
5. Run `scripts/deploy-pi.sh` from the development computer for each later release.

## Tests

| Command | Content |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests` | Unit, protocol conformance and end-to-end tests. No hardware is necessary. |
| `DOG_SERIAL=<port> .venv/bin/python -m unittest -v tests.test_conformance` | The same protocol tests against the real IR bridge. |
| `arduino-cli compile --fqbn esp32:esp32:adafruit_feather_esp32_v2 --warnings all firmware/ir_bridge` | Compiles the firmware. A warning is an error. |

CI runs the Python tests on Python 3.11 and 3.13, compiles the firmware, and checks the deploy files. CI machines are shared, so CI latency gates only find a large regression. `docs/test-log.md` records the measured values.

## Project structure

~~~text
robot-dog/
  pi/            Pi daemons: robotd, mediad, padd, linkd, robotctl, and the bus (dogproto)
  firmware/      ESP32 IR bridge firmware
  hub/           Server process: device link, robot tools, voice loop
  sim/           Simulated IR bridge, simulated Hermes, dev stack
  tests/         Unit, conformance and end-to-end tests
  config/        dog.toml and the registry template
  deploy/        systemd units and templates for the Pi and the server
  hooks/         Release hooks that run before and after an update
  scripts/       Package, install and deploy scripts
  docs/          Design documents, runbook and test log
~~~

## Safety

- Only a person at the Pi can arm the dog. The cloud can never arm it.
- robotd is the only process that talks to the IR bridge.
- Stop has priority over all motion. A stop also blocks the stick until the stick is back at center.
- Each command has a time limit. The bridge stops IR output when robotd goes silent.
- The result of an action keeps "transmitted" separate from "observed". An IR frame does not prove that the toy moved.
- An IR stop is not a dependable emergency stop. Keep the toy power switch in reach.

`docs/design/safety.md` gives all rules and limits.

## Documentation

| Document | Content |
| --- | --- |
| `docs/design/safety.md` | Arm rules, limits, priority, stop semantics, watchdogs. |
| `docs/design/protocol.md` | The local bus and the serial protocol of the IR bridge. |
| `docs/design/registry.md` | The registry format, its rules and the capture procedure. |
| `docs/design/media.md` | Camera, markers, audio, LAN console and gamepad. |
| `docs/design/cloud.md` | The Pi link, the robot tools and the voice loop. |
| `docs/design/release.md` | The release format, the health gate and the rollback. |
| `docs/design/echo.md` | The Atom Echo device: toy checks, parts, pins, methods, safety rules and plan. |
| `docs/runbook.md` | Procedures for the server and the Pi. |
| `docs/test-log.md` | Measured test results. |
| `firmware/README.md` | Toolchain, flash procedure and wire connections. |

Each design document owns one mechanism. When a document and the code disagree, correct the document first.

## Contributions

1. Open a GitHub issue for the change.
2. Make a branch from the top of the pull request stack.
3. Keep each pull request near 200 lines, with its tests.
4. Write `Closes #<issue>` in the pull request body.
5. Use these body sections: Why, What, How to Review, Test Evidence.
6. Run the tests before you push.

## Security

Do not open a public issue for a security problem. Send a private report through [GitHub security advisories](https://github.com/asigdel29/robot-dog/security/advisories/new).

Never commit a token, a key or an env file. The hub and Hermes listen only on loopback. Caddy makes only `/device` and `/healthz` public.

## License

This project uses the MIT License. See [LICENSE](LICENSE).

## Acknowledgements

- [microduck](https://github.com/pollen-robotics/microduck) by Pollen Robotics (Apache-2.0): the architecture model.
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research (MIT): the agent on the server.
- [Arduino-IRremote](https://github.com/Arduino-IRremote/Arduino-IRremote) and [ArduinoJson](https://arduinojson.org/): IR and JSON on the ESP32.
- [OpenCV](https://opencv.org/): ArUco marker detection.
- [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk): the robot tool server.
