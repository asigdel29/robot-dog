# Protocols

This document owns the wire formats between the processes. When this document and the code disagree, correct this document first. Then correct the code.

## 1. Local bus on the Pi

The code is in `pi/dogproto.py`.

- The format is JSON-RPC 2.0. Each message is one JSON object on one line.
- The transport is a unix socket in `/run/dog/`. The `DOG_RUN_DIR` variable changes this directory for tests.
- There is no broker. Each daemon serves its own sockets.
- `API_VERSION` is 1. A peer with a different value is logged. It is not refused.

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
- An unknown field, an absent field or a wrong type gets INVALID_PARAMS (-32602).
- A field name with a final `?` in the table is optional.

### 1.4 Error codes

| Code | Name | Meaning |
| --- | --- | --- |
| -32001 | NOT_ARMED | The dog is not armed. A person at the Pi must press Start. |
| -32002 | BUSY | An action is in progress, or the local stick has control. |
| -32003 | FAULT | robotd or the bridge is in FAULT. |
| -32004 | UNAVAILABLE | The command is not verified, not enabled for remote use, or the bridge link is down. |
| -32005 | STALE | The request is too old (linkd only). |
| -32006 | TIMEOUT | The bridge did not answer. |
| -32007 | PREEMPTED | A stop, a disarm or the local stick ended the action. |

### 1.5 robotd methods

| Method | Type | Cloud | Notes |
| --- | --- | --- | --- |
| `robot.move {vx, vyaw}` | Notification | No | Values in [-1, 1]. Deadman: 500 ms. |
| `robot.observe {t_mono_ns, markers}` | Notification | No | From `mediad`. `markers` maps an ArUco id to `[cx, cy, heading_rad]`. |
| `robot.enable` | Request | No | Arms the dog. Clears a FAULT when the link is healthy. |
| `robot.disarm` | Request | Yes | Cancels the action and disarms. |
| `robot.stop` | Request | Yes | Stops IR output now. Cancels the action. The dog stays armed. |
| `robot.do {trick, id}` | Request | Yes | Replies when the trick ends. |
| `robot.drive {dir, ms, id}` | Request | Yes | `dir` is forward, back, left or right. `ms` is 50 to the command `max_run_ms`. |
| `robot.goto {marker, id}` | Request | Yes | 30 s limit. Needs verified forward and one turn. |
| `robot.state` | Request | Yes | A cloud caller sees only cloud tricks. |
| `robot.capture {ms}` | Request | No | DISARMED only. Returns the IR frames that the receiver saw. |
| `robot.test {name, ms}` | Request | No | Armed only. Accepts a command that is not verified. |
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
| `hello` | `sid` | Starts a new link. The bridge goes to DISARMED and forgets its entries. |
| `load` | `id, name, proto, addr, cmd, bits, raw, khz, repeat, period_ms` | DISARMED only. Adds one entry to bridge RAM. |
| `arm` | `id, n` | `n` must equal the number of loaded entries. The bridge goes to READY. |
| `send` | `id, name` | READY only. Sends one frame. |
| `hold` | `id, name, lease_ms` | READY or EXECUTING. `lease_ms` is 1 to 400. A hold of the same name extends the lease. |
| `stop` | `id` | Stops repeat frames. |
| `disarm` | `id` | Stops repeat frames. The bridge goes to DISARMED. |
| `hb` | `seq` | Heartbeat. robotd sends it each 200 ms. |
| `capture` | `id, ms` | DISARMED only. Records frames for `ms`. |

### 2.2 Events (bridge to robotd)

| Event | Fields | Meaning |
| --- | --- | --- |
| `hello` | `sid, boot, fw, state` | Reply to `hello`. After a reset, the bridge sends one `hello` with `sid` 0. |
| `ack` / `nack` | `id` (and `why`) | The op was accepted or refused. |
| `tx` | `id, frames` | Frames sent for that op. |
| `done` | `id` | A `send` frame, a hold or a capture ended. |
| `state` | `s, max_loop_us, seq` | Reply to `hb`. `s` is DISARMED, READY, EXECUTING, STOPPING or FAULT. |
| `fault` | `why` | The bridge went to FAULT. |
| `cap` | `proto, addr, cmd, bits, raw, gap_us, repeat` | One captured frame. |

### 2.3 Bridge rules

1. When a lease expires, the bridge stops the repeat frames. This is the firmware deadman.
2. When no `hb` arrives for 1000 ms while armed, the bridge stops output. It sends the entry with the name `stop` if one is loaded. Then it goes to FAULT.
3. Only `arm` moves the bridge out of FAULT.
4. A port open toggles DTR and resets the Feather. This is acceptable, because a new link always starts DISARMED.
5. robotd sends the registry to the bridge at each handshake. A new capture never needs a new firmware.
