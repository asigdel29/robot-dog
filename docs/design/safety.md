# Safety

This document owns the rules that keep the dog from unwanted motion. When this document and the code disagree, correct this document first. Then correct the code.

## 1. Rules from microduck

1. `robotd` is the only process that opens the IR bridge serial port.
2. `robotd` makes no outbound connection. Its loop never waits for another service.
3. Each state has one writer. `robotctl` writes the registry. `robotd` writes the arm state. `mediad` writes the pose.
4. Start-up never moves the dog. After a boot, a reset, a reconnect or a new release, the dog is DISARMED.
5. Continuous intents have a deadman. Discrete actions have a limit.

## 2. Arm state

- Only a local client can arm the dog. `robot.enable` is not in `CLOUD_METHODS`.
- The arm controls are gamepad Start and the LAN console.
- robotd arms the dog only with a ready registry. A registry that breaks a rule loads no entry into the bridge.
- The armed state expires after 10 minutes with no action and no stick input.
- When the bridge link fails or the bridge resets while the dog is armed, robotd disarms the dog and goes to FAULT.
- robotd disarms the dog when it stops.
- `linkd` disarms the dog when the WSS link to the hub fails.
- A reconnect never arms the dog again.
- A disarm or a bridge reset that arrives during `robot.enable` wins. The dog stays DISARMED.

## 3. Motion limits

| Limit | Value | Where |
| --- | --- | --- |
| Loop rate | 20 Hz | robotd |
| Stick deadman | 500 ms after the last `robot.move` | robotd |
| Hold lease from robotd | 300 ms, renewed each 100 ms for an action and each 50 ms for the stick | robotd |
| Hold lease cap | 400 ms | bridge |
| Heartbeat | each 200 ms | robotd |
| Heartbeat timeout | 1000 ms, then FAULT | bridge |
| Bridge silence | 1000 ms, then the link is down | robotd |
| Hold command run | `max_run_ms`, 1000 ms or less | registry |
| Cloud episodic trick | `natural_ms`, 10000 ms or less | registry |
| goto | 30 s, 20 steps, 5 forward steps with no progress | robotd |
| Vision age for goto | 2 s | robotd |

There is no raw IR method and no free duration. The registry gives the limit for each command.

## 4. Priority

1. `robot.stop` and `robot.disarm`. Each one also latches the stick: robotd ignores stick input until the stick is back at center. Thus a stick that stays pushed cannot cancel a stop.
2. Local stick input. It cancels a cloud action in progress with PREEMPTED.
3. `robot.do`, `robot.drive` and `robot.goto`. Only one runs at a time.

An action belongs to the connection that sent it. When that connection closes, robotd cancels the action.

## 5. Stop semantics

`robot.stop` does these steps:

1. It sends the `stop` op to the bridge. The bridge stops repeat frames.
2. It cancels the action in progress.
3. For a `perpetual` trick, or an early stop of an episodic trick, robotd sends the command in `stop_via`.
4. It replies `stop_transmitted`. The camera result is separate.

An IR stop is not a dependable emergency stop. The IR path can be blocked, the toy can ignore it, and the bridge can lose power. The toy power switch is the dependable manual stop.

## 6. Watchdogs

| Failure | Result | Measured in the dev stack |
| --- | --- | --- |
| `kill -9 robotd` during a hold | The lease ends the repeat frames. The heartbeat timeout puts the bridge in FAULT. | Last repeat 145 to 156 ms after the kill (lease 300 ms). FAULT 958 ms after the kill, after the `stop` frame. The test limit is 1.3 s after the last heartbeat: 1000 ms watchdog, 40 ms gap, a 70 ms stop frame and slack for a busy host. |
| robotd loop stops | systemd `WatchdogSec=2` restarts robotd. The restart starts DISARMED. | Not measured yet |
| Ten tick errors in sequence | robotd goes to FAULT. | Not tested |
| Loop rate under 15 Hz for 5 s | Health is `degraded`. | Not tested |

## 7. Results

Each action result keeps `transmitted` separate from `observed`. Hermes must never say that a move succeeded when only `transmitted` is true.
