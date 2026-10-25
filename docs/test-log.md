# Test log

Record each test result here: the date, the command, the setup and the measured value. Do not record a result that you did not measure.

| Date | Phase | Command | Setup | Result |
| --- | --- | --- | --- | --- |
| 2026-10-25 | 1 | `python -m unittest discover -s tests` (3 runs) | MacBook, Python 3.11.15, fake bridge | 51 tests pass in each run. Stop latency p99: 12.97, 0.91, 1.51 ms (target 75 ms). After `kill -9 robotd` in a hold: last repeat 130, 235, 142 ms (limit 400 ms); FAULT 821, 824, 823 ms (limit 1100 ms). Simulated goto: 10 of 10 goals. |
| 2026-10-25 | 1 | `python -m unittest discover -s tests` (2 runs, after review fixes) | MacBook, Python 3.11.15, fake bridge | 62 tests pass in each run. Stop latency p99: 2.81, 0.61 ms. Owner close to stop: 0.2, 0.4 ms. After `kill -9 robotd` in a hold: last repeat 157, 163 ms; FAULT 822, 825 ms. |
| 2026-10-25 | 2 | `arduino-cli compile --fqbn esp32:esp32:adafruit_feather_esp32_v2 --warnings all firmware/ir_bridge` | MacBook, arduino-cli 1.5.1, esp32 core 3.3.12, IRremote 4.7.1, ArduinoJson 7.4.3 | No compiler warnings. Program storage: 952640 bytes (28 %). Global variables: 56348 bytes (17 %). |
| 2026-10-25 | 2 | `python -m unittest discover -s tests` | MacBook, Python 3.11.15, fake bridge | 66 tests pass. Conformance on the real board is not done: no hardware yet (phase 5). |
| 2026-10-25 | 2 | `arduino-cli compile --fqbn esp32:esp32:adafruit_feather_esp32_v2 --warnings all firmware/ir_bridge` (after review) | MacBook, arduino-cli 1.5.1, esp32 core 3.3.12, IRremote 4.7.1, ArduinoJson 7.4.3 | No compiler warnings. Program storage: 341840 bytes (10 %). Global variables: 32588 bytes (9 %). |
| 2026-10-25 | 2 | `python -m unittest discover -s tests` (2 runs, after review) | MacBook, Python 3.11.15, fake bridge with frame times | 73 tests pass in each run. Stop latency p99: 1.30, 5.11 ms. Owner close to stop: 0.3, 0.2 ms. After `kill -9 robotd` in a hold: last repeat 156, 145 ms; FAULT 958, 958 ms (the fake now sends the `stop` frame, about 70 ms, before `fault`). |
