# Protocols

This document owns the wire formats between the processes. When this document and the code disagree, correct this document first. Then correct the code.

## 1. Local bus on the Pi

The code is in `pi/dogproto.py`.

- The format is JSON-RPC 2.0. Each message is one JSON object on one line.
- The transport is a unix socket in `/run/dog/`. The `DOG_RUN_DIR` variable changes this directory for tests.
- There is no broker. Each daemon serves its own sockets.
- `API_VERSION` is 1. A peer with a different value is logged. It is not refused.
- The line limit is 64 KiB for robotd and 2 MiB for mediad. A longer line gets PARSE_ERROR (-32700), and the daemon closes the connection.

### 1.1 Two sockets for each daemon

| Socket | Group | Clients |
| --- | --- | --- |
| `robotd.sock`, `mediad.sock` | `dog` | Processes on the Pi: `padd`, `mediad`, `robotctl`, the LAN console |
| `robotd-cloud.sock`, `mediad-cloud.sock` | `doglink` | `linkd` only |

File permissions (mode 0660 and the group) decide who can connect. A cloud socket also refuses each method that is not in `CLOUD_METHODS`. It returns METHOD_NOT_FOUND.

### 1.2 Message families

- A continuous intent is a notification. It has no `id` and gets no reply. The last value wins. Example: `robot.move`.
- A discrete action is a request. It has an `id` and gets one reply. Example: `robot.do`.
- Each request runs in its own task. Thus a `robot.stop` can overtake a `robot.goto` on the same connection.
- When a connection closes, the daemon cancels the open requests of that connection.

### 1.3 Validation

The `PARAMS` table in `dogproto.py` is the reference for each method and its fields.

- An unknown method gets METHOD_NOT_FOUND (-32601).
- An unknown field, an absent field or a wrong type gets INVALID_PARAMS (-32602). A bool is not a number.
- A field name with a final `?` in the table is optional.
- The bus refuses `NaN` and `Infinity`, because a clamp changes `NaN` into full motion. A line with `NaN` gets PARSE_ERROR. A number such as `1e999` gets INVALID_PARAMS.

### 1.4 Error codes

| Code | Name | Description |
| --- | --- | --- |
| -32001 | NOT_ARMED | The dog is not armed. A person at the Pi must press Start. |
| -32002 | BUSY | An action or a capture is in progress, the local stick has control, or the method needs DISARMED. |
| -32003 | FAULT | robotd or the bridge is in FAULT, or the bridge refused an op. |
| -32004 | UNAVAILABLE | The registry is not ready, the command is not verified or not enabled for remote use, or the bridge link is down. |
| -32005 | STALE | The request is too old (linkd only). |
| -32006 | TIMEOUT | The bridge did not answer. |
| -32007 | PREEMPTED | A stop, a disarm or the local stick ended the action, or a disarm overtook `robot.enable`. |

### 1.5 robotd methods

| Method | Type | Cloud | Notes |
| --- | --- | --- | --- |
| `robot.move {vx, vyaw}` | Notification | No | Values in [-1, 1]. Deadman: 500 ms. |
| `robot.observe {t_mono_ns, markers}` | Notification | No | From `mediad`. `markers` maps an ArUco id to `[cx, cy, heading_rad]`. |
| `robot.enable` | Request | No | Arms the dog and clears a FAULT. Needs a ready registry and the bridge link. |
| `robot.disarm` | Request | Yes | Cancels the action and disarms. |
| `robot.stop` | Request | Yes | Stops IR output now. Cancels the action. The dog stays armed. |
| `robot.do {trick, id}` | Request | Yes | Replies when the trick ends. |
| `robot.drive {dir, ms, id}` | Request | Yes | `dir` is forward, back, left or right. `ms` is 50 to the command `max_run_ms`. |
| `robot.goto {marker, id}` | Request | Yes | 30 s limit. Needs verified forward and one turn. |
| `robot.state` | Request | Yes | A cloud caller sees only cloud tricks and cloud capabilities. |
| `robot.capture {ms}` | Request | No | DISARMED only, one at a time. `ms` is 100 to 10000. Returns the IR frames that the receiver saw. |
| `robot.test {name, ms}` | Request | No | Armed only. `ms` is 50 to 1000. Accepts a command that is not verified. |
| `robot.reload` | Request | No | DISARMED only. Reads the registry again and loads the bridge again. |

An action result has this shape:

~~~json
{"transmitted": true, "frames": 4, "observed": {"moved_px": 31.5, "turned_deg": 0.4}}
~~~

`observed` is `null` when the camera pose is absent or older than 2 s. `transmitted` only means that the bridge sent IR frames. It does not mean that the toy moved.

A repeated action `id` returns the first result. The dog does not move again. robotd keeps the last 256 ids.

## 2. robotd to the IR bridge (USB serial)

The firmware is `firmware/ir_bridge/ir_bridge.ino`. The fake is `sim/fake_esp32.py`. `tests/test_conformance.py` checks both.

- The format is one JSON object on one line. The limit is 1024 bytes. The speed is 115200 baud.
- robotd sends ops with an `op` key. The bridge sends events with an `ev` key.
- Each op except `hello` and `hb` has an `id`. Each `id` must be larger than the last `id` after the last `hello`.

### 2.1 Ops (robotd to bridge)

| Op | Fields | Rule |
| --- | --- | --- |
| `hello` | `sid` | Starts a new link. The bridge goes to DISARMED. It forgets its entries, its hold and its capture, and sends no `done` for them. |
| `load` | `id, name, proto, addr, cmd, raw, khz, repeat, period_ms`, optional `data` | DISARMED only. Adds one entry to bridge RAM, or replaces the entry with the same name. See 2.4 for the limits. |
| `arm` | `id, n` | `n` must equal the number of loaded entries. From DISARMED or FAULT, the bridge goes to READY. An armed bridge keeps its state. Refused while a capture runs. |
| `send` | `id, name` | READY only. Sends one full frame after the gap (2.3 rule 7). The bridge applies the next op after the `done` of the send. |
| `hold` | `id, name, lease_ms` | READY or EXECUTING. `lease_ms` is an integer from 1 to 400. A hold of the same name extends the lease. A hold of another name replaces the hold. See 2.3 rule 8. |
| `stop` | `id` | Stops repeat frames. |
| `disarm` | `id` | Stops repeat frames. The bridge goes to DISARMED. |
| `hb` | `seq` | Heartbeat. robotd sends it each 200 ms. |
| `capture` | `id, ms` | DISARMED only. Records frames for `ms`, an integer (`why` `bad ms` when not). One capture at a time. |

### 2.2 Events (bridge to robotd)

| Event | Fields | Description |
| --- | --- | --- |
| `hello` | `sid, boot, fw, state` | Reply to `hello`. After a reset, the bridge sends one `hello` with `sid` 0. `boot` changes at each reset. |
| `ack` / `nack` | `id` (and `why`) | The op was accepted or refused. |
| `tx` | `id, frames` | Frames sent for that op. |
| `done` | `id` | A `send` frame, a hold or a capture ended. Rule 9 gives the cases with no `done`. |
| `state` | `s, max_loop_us, seq` | Reply to `hb`. `s` is DISARMED, READY, EXECUTING or FAULT. |
| `fault` | `why` | The bridge went to FAULT. |
| `cap` | `proto, addr, cmd, bits, data, raw, gap_us, repeat` | One captured frame. See 2.5. |

### 2.3 Bridge rules

1. When a lease expires, the bridge stops the repeat frames. This is the firmware deadman.
2. When no `hb` arrives for 1000 ms while armed, the bridge stops output. It sends the entry with the name `stop` once if one is loaded (`tx` with `id` 0). Then it goes to FAULT and sends `fault`.
3. Only `arm` moves the bridge from FAULT to READY. `hello` and `disarm` move it to DISARMED.
4. A port open toggles DTR and resets the Feather. This is acceptable, because a new link always starts DISARMED.
5. robotd sends the registry to the bridge at each handshake. A new capture never needs a new firmware.
6. The receiver runs only during a capture. A capture needs DISARMED, and `arm` is refused while a capture runs. Thus the bridge never decodes its own frames.
7. Before the first frame of a new command, the emitter is quiet for 40 ms or more. Thus the toy sees a new press. A new command is a `send`, the first frame of a hold, or the watchdog `stop`.
8. The bridge always sends the first frame of a hold, also when the lease is shorter than the frame. A hold of another name keeps the frame times of the hold that it replaces.
9. The bridge sends no `done` in these cases:
   - A `hello` ends a hold or a capture.
   - The watchdog ends a hold or a `send` that waits for the gap.
   - A hold of another name replaces a hold.
10. One frame blocks the bridge for its full time, 150 ms or less. Ops that arrive meanwhile wait in the serial buffer. Their order does not change.

### 2.4 Entry limits

The bridge refuses a `load` that it cannot store or send. The `why` is `table full` or `bad entry`. Each field except `data` must be present with the correct JSON type. A number with a fraction, a negative number, a string or `null` gives `bad entry`. The bridge ignores other fields.

| Field | Limit |
| --- | --- |
| Entries | 24 (`table full`) |
| `name` | String of 1 to 31 bytes in UTF-8 |
| `proto` | `NEC` or `raw` |
| `addr`, `cmd` | Unsigned integers. For `NEC`: `addr` 0 to 0xFFFF, `cmd` 0 to 0xFF |
| `data` | Optional. Unsigned 32-bit integer. For `NEC`, the bridge sends these 32 bits as captured, not `addr` and `cmd`. |
| `raw` | Array of 120 durations or fewer, each an integer from 1 to 65535 µs. The total is 150 ms or less. A `raw` entry needs one or more. |
| `khz` | Integer from 20 to 60 |
| `repeat` | `nec`, `resend` or `none` |
| `period_ms` | Integer from 20 to 1000 |

robotd checks the same limits when it reads the registry (`check_registry`). Thus the bridge never refuses a `load` from robotd. With these limits, each `load` line is shorter than 1024 bytes.

### 2.5 Captured frames

- `proto` is `NEC`, another protocol name from the IRremote decoder, or `raw` when the decoder does not know the protocol.
- `data` is the decoded data. For `NEC`, it is the 32 bits on the air, least significant bit first. robotctl keeps it, so the replay is exact also for an extended address.
- `raw` gives the mark and space durations in microseconds. The first value is a mark. `raw` is empty when the frame has more than 120 durations.
- The receiver clips each mark and space at 12750 µs. A space longer than 8 ms ends a frame.
- `gap_us` is the time from the start of the frame before to the start of this frame. It is 0 for the first frame of a capture. robotctl uses it as `period_ms`.
- `repeat` is true for a short repeat frame with no data bits, for example the NEC repeat frame. A full frame that the remote sends again has `repeat` false.
