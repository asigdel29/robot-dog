# Trick registry

This document owns the registry format, its rules and the capture procedure. When this document and the code disagree, correct this document first. Then correct the code.

## 1. File

- The live file is `/etc/dog/registry.json`. It is outside the release.
- `robotctl` is the only writer. robotd only reads it.
- The repo keeps a template in `config/registry.json`. The postinstall hook copies it only when no live file exists.
- Raw captures go to `/var/lib/dog/captures/`. Keep them. They are the evidence for each entry.

## 2. Format

~~~json
{"schema": 1, "robot": {"model": "supireo-y8359", "id": "dog-01"},
 "commands": {"forward": {
   "ir": {"proto": "NEC", "addr": 0, "cmd": 24, "bits": 32, "raw_us": [], "carrier_khz": 38},
   "kind": "hold", "repeat": "nec", "period_ms": 108, "stop_via": "release",
   "natural_ms": null, "max_run_ms": 500, "interruptible": true,
   "verified": true, "cloud": true, "say": "walks forward", "notes": ""}},
 "drive": {"forward": "forward", "left": null, "right": null, "back": null},
 "goto": {"robot_marker": 1, "goal_radius_px": 40, "area_px": [[0, 0], [640, 0], [640, 480], [0, 480]],
          "turn_ms": 200, "forward_ms": 300, "settle_ms": 300, "turn_sign": 1}}
~~~

### 2.1 Command fields

| Field | Values | Meaning |
| --- | --- | --- |
| `ir.proto` | `NEC` or `raw` | `raw` replays `raw_us` timings. Protocols with a toggle bit are refused. |
| `kind` | `hold`, `episodic`, `perpetual`, `mode` | `hold`: motion while frames repeat. `episodic`: one routine with a natural end. `perpetual`: continues until a stop command. `mode`: changes a toy setting. |
| `repeat` | `nec`, `resend`, `none` | How the remote repeats a held button. |
| `period_ms` | integer | Time between repeat frames. |
| `stop_via` | `release`, a command name, `none` | How the motion ends. |
| `natural_ms` | integer or null | Measured time of one episodic routine. |
| `max_run_ms` | integer | Limit for a hold or perpetual command. |
| `interruptible` | true, false, null | Result of the stop test. |
| `verified` | bool | True only after capture and a physical replay test. |
| `cloud` | bool | True permits Hermes to use the command. |
| `say` | text | Short description for Hermes. |
| `notes` | text | Evidence: date, distance, surface, results. |

### 2.2 goto fields

| Field | Meaning |
| --- | --- |
| `robot_marker` | ArUco id on the dog. Attach it with its top edge toward the nose. |
| `goal_radius_px` | The goal is reached inside this radius. |
| `area_px` | The dog must stay inside this polygon (image pixels). |
| `turn_ms`, `forward_ms`, `settle_ms` | Pulse and wait times. Calibrate them on the real floor. |
| `turn_sign` | 1 when the camera looks straight down. Use -1 for a mirrored view. |

## 3. Rules at load time

When a rule fails, robotd runs, but `registry_ready` is false and robotd refuses to arm.

1. Refuse unknown fields.
2. A verified command must have an IR payload.
3. A verified `perpetual` command must have a `stop_via` that names a verified command.
4. A `hold` command must have `max_run_ms` from 1 to 1000.
5. A `cloud` command must be verified.
6. A `cloud` episodic command must have `natural_ms` of 10000 or less.
7. A `mode` command can never be a cloud command.
8. Each `drive` entry must name a `hold` command. An entry that names a command that is not verified is not an error. That direction is only not available.
9. `goto` is available only when forward and one turn are verified.

These rules replace "refuse to arm without stop" from the old plan. A toy with no stop button is safe when each enabled command is a hold command or a bounded episodic command.

## 4. Procedure: add one button

Do these steps for each of the 12 remote buttons.

1. Disarm the dog.
2. Run `robotctl registry capture <name>`.
3. Press the button once when the tool tells you. Do this three times.
4. Hold the button down for 4 s when the tool tells you.
5. Arm the dog with gamepad Start.
6. Run `robotctl registry test <name> --ms 300`. Watch the dog.
7. Disarm the dog. robotd reads a new registry only while it is DISARMED.
8. Record the behavior with `robotctl registry verify <name> --kind <kind> --natural-ms <ms> --interruptible <yes|no> --notes "<evidence>"`.
9. Add `--cloud yes` only after the stop test passes at operating distance.
10. For a drive button, run `robotctl registry map <dir> <name>`.

When the dog is armed, `verify` and `map` write the file and report `reload: pending`. Disarm the dog. Then run `robotctl registry reload`.
