# robot-dog

[![test](https://github.com/asigdel29/robot-dog/actions/workflows/test.yml/badge.svg)](https://github.com/asigdel29/robot-dog/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

This project gives a voice and an agent to a toy robot dog. A Raspberry Pi sends the same infrared (IR) commands as the toy remote. A Hermes Agent on a server listens, talks, does web tasks and starts verified tricks.

## Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Tests](#tests)
- [Safety](#safety)
- [Documentation](#documentation)

## Overview

The toy is a SUPIREO Y8359 robot dog. It has an IR remote with 12 buttons and small wheels under its feet. It has no servos and no interface for its joints. Thus this project controls the toy at the level of its remote buttons.

The software architecture follows [microduck](https://github.com/pollen-robotics/microduck) by Pollen Robotics:

- One daemon for each concern.
- JSON-RPC over unix sockets.
- One writer for each state.
- A deadman on continuous motion.
- A start-up that never moves the dog.
- Releases with a health gate.

This repo copies no microduck code.

Status: the software works against a simulated IR bridge and a simulated toy. The hardware phases are open. The GitHub issues give the full plan and its order.

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

## Tests

| Command | Content |
| --- | --- |
| `.venv/bin/python -m unittest discover -s tests` | Unit, protocol conformance and end-to-end tests. No hardware is necessary. |
| `DOG_SERIAL=<port> .venv/bin/python -m unittest -v tests.test_conformance` | The same protocol tests against the real IR bridge. |
| `arduino-cli compile --fqbn esp32:esp32:adafruit_feather_esp32_v2 --warnings all firmware/ir_bridge` | Compiles the firmware. A warning is an error. |

CI runs the Python tests on Python 3.11 and 3.13, compiles the firmware, and checks the deploy files. CI machines are shared, so CI latency gates only find a large regression. `docs/test-log.md` records the measured values.

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
| `docs/runbook.md` | Procedures for the server and the Pi. |
| `docs/test-log.md` | Measured test results. |
| `firmware/README.md` | Toolchain, flash procedure and wire connections. |

Each design document owns one mechanism. When a document and the code disagree, correct the document first.
