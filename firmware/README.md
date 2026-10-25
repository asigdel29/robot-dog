# IR bridge firmware

This folder contains the firmware for the IR bridge. The bridge is an Adafruit ESP32 Feather V2 (#5900) with an Adafruit IR transceiver (#5990). robotd sends commands to the bridge over USB serial. The bridge sends the IR frames to the toy dog. The firmware never starts WiFi or Bluetooth.

`docs/design/protocol.md` section 2 gives the wire protocol. `sim/fake_esp32.py` is the reference behavior. `tests/test_conformance.py` checks the fake and the board.

## Versions

| Part | Version |
| --- | --- |
| arduino-cli | 1.5.1 |
| esp32 core (Espressif) | 3.3.12 |
| IRremote | 4.7.1 |
| ArduinoJson | 7.4.3 |
| Board (FQBN) | `esp32:esp32:adafruit_feather_esp32_v2` |

The CI job in `.github/workflows/test.yml` uses the same versions. When you change a version, change it in the two files.

## Install the toolchain

1. Run `curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR="$HOME/.local/bin" sh -s 1.5.1`.
2. Add `$HOME/.local/bin` to `PATH`.
3. Run `arduino-cli version`.
4. Make sure that the version is 1.5.1.
5. Run `arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json`.
6. Run `arduino-cli core install esp32:esp32@3.3.12 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json`.
7. Run `arduino-cli lib install IRremote@4.7.1 ArduinoJson@7.4.3`.

Homebrew (`brew install arduino-cli`) installs the latest version, not 1.5.1. If you use it, do steps 3 and 4.

## Compile

1. Open a terminal in the repo root.
2. Run `arduino-cli compile --fqbn esp32:esp32:adafruit_feather_esp32_v2 --warnings all firmware/ir_bridge`.

The compiler must show no warnings. The CI job fails when a warning occurs.

The last result was 341840 bytes of program storage (10 % of 3342336) and 32588 bytes of global variables (9 % of 327680).

## Connect the IR module

| IR module pad | Feather V2 pad | Function |
| --- | --- | --- |
| `VIN` | `3V` | 3.3 V supply, the same as the ESP32 logic voltage |
| `GND` | `GND` | Common ground |
| `IRin` | `33` (GPIO33) | Receiver output into the ESP32 |
| `IRout` | `27` (GPIO27) | ESP32 output into the emitter driver |

1. Disconnect the USB cable from the Feather.
2. Connect each module pad to the Feather pad in the table.
3. Identify each wire by the labeled pads at its two ends.
4. Do a continuity test on each wire with a multimeter.

Do not identify a wire by its color. The Adafruit documents do not agree about the wire colors. Do not connect 5 V to an ESP32 pin.

## Find the port

1. Connect the Feather to the computer with a USB data cable.
2. Run `arduino-cli board list`.

On macOS, the port has a name such as `/dev/cu.usbserial-XXXX` or `/dev/cu.wchusbserial-XXXX`. On Linux, the port has a name such as `/dev/ttyUSB0`. On Linux, your user must be in the `dialout` group. On the Pi, use the persistent path that `ls -l /dev/serial/by-id/` shows.

## Write the firmware to the board

1. Compile the firmware. See "Compile".
2. Run `arduino-cli upload -p <port> --fqbn esp32:esp32:adafruit_feather_esp32_v2 firmware/ir_bridge`.

The upload resets the board. After a reset, the bridge sends one `hello` line with `sid` 0. It starts in DISARMED and does not transmit IR.

## Run the conformance tests on the board

The tests send a test NEC code (address 0, command 1). The toy can possibly react to this code.

1. Set the toy power switch to off.
2. Close each program that uses the port, for example a serial monitor.
3. Open a terminal in the repo root.
4. Run `DOG_SERIAL=<port> python -m unittest -v tests.test_conformance`.

Each test opens the port, and the port open can reset the board. Thus each test waits up to 6 s for a `hello` reply.

## Calibration

Three values depend on the real remote and the toy. Keep the given values until a test on the real toy shows a problem.

- `MARK_EXCESS_MICROS` (IRremote, default 20): the receiver module makes each mark longer by this time. The firmware removes this time from captured marks.
- `RECORD_GAP_MICROS` (IRremote, default 8000): a space longer than this time ends a frame. Increase it when one button press gives two frames.
- `MIN_GAP_MS` (40): the emitter is quiet for this time before the first frame of a new command. Increase it when the toy ignores the second of two commands.

To change an IRremote value, add a `#define` line before `#include <IRremote.hpp>`. When you change `MIN_GAP_MS`, change `MIN_GAP_S` in `sim/fake_esp32.py` too.
