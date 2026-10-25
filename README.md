# robot-dog

This repo gives a voice and an agent to a SUPIREO Y8359 toy robot dog. The toy has only an infrared (IR) remote. A Raspberry Pi and an ESP32 IR bridge send the same IR frames as the remote. A Hermes Agent on a server listens, talks, does web tasks and starts verified tricks.

The software architecture follows [microduck](https://github.com/pollen-robotics/microduck) by Pollen Robotics (Apache-2.0): one daemon for each concern, JSON-RPC over unix sockets, one writer for each state, a deadman, start-up that never moves, and health-gated releases. This repo copies no microduck code.

## Parts

| Part | Where | Job |
| --- | --- | --- |
| `pi/robotd.py` | Pi | Only process that talks to the IR bridge. 20 Hz loop, registry, arm state, actions, safety. |
| `pi/robotctl.py` | Pi | Command-line control and the only writer of the registry. |
| `pi/mediad.py` | Pi | Camera, ArUco pose, microphone, speaker and the LAN console. Only writer of the pose. |
| `pi/padd.py` | Pi | Wired USB gamepad: arm, stop, drive, push-to-talk and bound tricks. |
| `pi/dogproto.py` | Pi | The local bus: JSON-RPC 2.0, one object per line, two sockets for each daemon. |
| `firmware/ir_bridge/` | ESP32 Feather V2 | IR capture and replay, leases, heartbeat watchdog. |
| `sim/fake_esp32.py` | Laptop, CI | The bridge protocol on a pty, with a simulated toy. |
| `sim/devstack.py` | Laptop, CI | Starts the stack against the fake bridge. |

The design documents in `docs/design/` own the mechanisms:

- `protocol.md`: the wire formats.
- `safety.md`: arm rules, limits, stop semantics, watchdogs.
- `registry.md`: the trick registry and the capture procedure.
- `media.md`: the camera, the markers, the pose, audio, the LAN console and the gamepad.

## Run the tests

1. Install Python 3.11 or later.
2. Run `python -m pip install -r requirements-dev.txt` (OpenCV and NumPy).
3. Run `python -m unittest discover -v -s tests` from the repo root.

The tests need no hardware. mediad uses a fake camera and fake audio in the tests. `tests/test_conformance.py` runs against the real bridge when `DOG_SERIAL` names its port:

~~~bash
DOG_SERIAL=/dev/cu.usbserial-XXXX python -m unittest -v tests.test_conformance
~~~

## Run the dev stack

1. Run `python sim/devstack.py`. It prints a run directory and the URL of the LAN console.
2. In a second terminal, set `DOG_RUN_DIR` to that directory.
3. Run `python pi/robotctl.py --config /dev/null state`.
4. Run `python pi/robotctl.py --config /dev/null enable`.
5. Run `python pi/robotctl.py --config /dev/null do bark`.

## Safety

- Only a person at the Pi can arm the dog. The cloud can never arm it.
- An IR stop is not a dependable emergency stop. Keep the toy power switch in reach.
- `transmitted: true` only means that IR frames left the bridge. The camera result is `observed`.
