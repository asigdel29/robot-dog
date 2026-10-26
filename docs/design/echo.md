# Atom Echo device

This document owns the design of the Atom Echo device. The code for it is not written yet. Issue #143 holds the work. When this document and the code disagree, correct this document first.

## 1. Toy checks

These checks were done on 2026-10-25:

- The USB-C port of the toy only charges its battery. A Mac shows no USB device when the toy is on.
- The main board is in the head. Its controller is a chip under an epoxy blob. Its program is fixed.
- The main board has no antenna, no shield and no radio module. Thus the toy has no Wi-Fi and no Bluetooth.
- The IR receiver is on a small board at the front of the head. A red, black and white cable connects it to the main board.
- The remote has 19 buttons, not 12.

Result: the IR receiver is the only input of the toy. A new board must send the remote codes.

| Group | Buttons |
| --- | --- |
| Arrows | up, down, left, right |
| Center | stop and play (■/▶▶) |
| Tricks | Lovely, Anger, Sit Down, Handstand, Get Down, Funny Play, Yoga, Imitation, Dancing, Push-Ups, Follow |
| Mode | Programming, volume +, volume − |

Programming records a sequence of buttons in the toy. A mode button can never be a cloud command.

## 2. Parts

| Part | Job | Price | Bought from | Other store |
| --- | --- | --- | --- | --- |
| M5Stack Atom Echo (SKU C008-C) | Wi-Fi, microphone, speaker, button, RGB LED | about $14 | [Amazon B0C7QSVPB2](https://www.amazon.com/dp/B0C7QSVPB2) | [Hackster](https://hackster.io/products/buy/74003) |
| M5Stack Unit IR (SKU U002), with a Grove cable | IR emitter (5 m or less) and 38 kHz IR receiver | about $4 | [Amazon B08TCC28VZ](https://www.amazon.com/dp/B08TCC28VZ) | [Hackster](https://hackster.io/products/buy/71896) |
| USB-C data cable | Flashes the board from the Mac | Owned | | |
| 5 V USB charger | Supplies power to the board | Owned | | |

Amazon gives the Atom Echo the name "Atom Voice Smart Speaker Dev Kit". The Pi, the Feather, the C270, the USB speaker and the gamepad are not necessary now. Issue #144 holds the camera phase.

## 3. Architecture

~~~mermaid
flowchart LR
  LAPTOP["Laptop: setup only"] -->|USB| ECHO
  ECHO["Atom Echo + Unit IR: registry, arm, limits, audio"] -->|IR| DOG["Toy dog"]
  ECHO <-->|WSS| HUB["hub on the droplet"]
  HUB <-->|loopback| HERMES["Hermes Agent"]
~~~

- The board runs the action engine. It keeps the registry, the arm state and the limits. The hub cannot change them.
- The hub keeps its cloud method names. Thus the robot tools and the voice loop change only a little.
- A laptop tool does the setup over USB: Wi-Fi, hub address, registry, capture and tests.

| Microduck part | Pi path | Atom Echo path |
| --- | --- | --- |
| `robotd` | `pi/robotd.py` | Board action engine |
| `mediad` audio | `pi/mediad.py` | Board microphone and speaker |
| `padd` | `pi/padd.py` | Board button |
| `btd` and the remote gateway | `pi/linkd.py` | Board WSS link |
| `duckctl` | `pi/robotctl.py` | `tools/echoctl.py` on the laptop |

## 4. Board pins

| Function | Pin | Note |
| --- | --- | --- |
| IR emitter (Unit IR, yellow wire) | G26 | Confirm at the first flash. When capture fails, swap G26 and G32. |
| IR receiver (Unit IR, white wire) | G32 | |
| Button | G39 | Active low. |
| RGB LED (SK6812) | G27 | |
| I2S bit clock | G19 | Speaker only. |
| I2S word clock and PDM clock | G33 | The speaker and the microphone share it. |
| Speaker data (NS4168) | G22 | |
| Microphone data (SPM1423, PDM) | G23 | |

The board has one I2S port. Thus it cannot record and play at the same time.

## 5. Transports

| Transport | Format | Trust | Methods |
| --- | --- | --- | --- |
| USB serial, 115200 baud | JSON-RPC 2.0, one object per line, 4 KiB line limit | Local: a person is at the dog | All |
| WSS to `wss://dog.<domain>/device` | JSON-RPC 2.0 text frames, 32 KiB frame limit | Cloud | `robot.state`, `robot.do`, `robot.drive`, `robot.stop`, `robot.disarm`, `audio.play`, `audio.stop` |

- A cloud request for a different method gets METHOD_NOT_FOUND.
- The error codes are the codes in `protocol.md` section 1.4.
- The board sends `Authorization: Bearer <DEVICE_TOKEN>` when it connects.
- A cloud `robot.do` or `robot.drive` gets STALE when its `sent_at` is more than 1500 ms from the board clock.
- The board clock comes from SNTP. Before the first SNTP sync, the board refuses cloud motion.

## 6. Methods

| Method | Transport | Rule |
| --- | --- | --- |
| `config.set {ssid, password, url, token}` | USB | Writes NVS, disarms and connects again. `url` is `wss://`, or `ws://` with a private IPv4 or `.local` host. |
| `config.get` | USB | Returns `ssid`, `url` and `token_set`. It never returns the password or the token. |
| `reg.clear`, `reg.put {name, command}`, `reg.drive {forward, left, right, back}`, `reg.commit` | USB | DISARMED only. `command` has the format of `registry.md`. `reg.commit` checks the rules of `registry.md` section 3 again, then writes NVS. |
| `ir.capture {ms}` | USB | DISARMED only. `ms` is 10000 or less. Sends one `ir.frame` notification for each frame, with the fields of the bridge `cap` event. Then it replies. |
| `robot.enable` | USB | Arms the dog. |
| `robot.test {name, ms}` | USB | READY only. `ms` is 1000 or less. |
| `robot.state` | Both | Returns `api`, `kind: "echo"`, `armed`, `busy`, `registry_ready`, `tricks`, `caps`, `rssi_dbm`, `uptime_s`. `caps.goto` and `caps.look` are false. |
| `robot.do {trick, id}` | Both | Runs one verified trick. The cloud needs `cloud: true`. |
| `robot.drive {dir, ms, id}` | Both | Holds one drive direction. `ms` is 50 to `max_run_ms`. |
| `robot.stop`, `robot.disarm` | Both | As in `protocol.md` section 1.5. |
| `audio.play {pcm16_b64, rate}`, `audio.stop` | Both | `rate` is 24000. |

The board sends these notifications to the hub:

| Notification | Content |
| --- | --- |
| `link.hello` | `api: 1`, `version`, `device: "dog-01"`, `kind: "echo"`, `play_buffer_ms` |
| `voice.start`, `voice.chunk`, `voice.end` | `id`, then `rate: 16000`, then `seq` and `pcm16_b64` for each 100 ms chunk |
| `voice.cancel` | No fields. |

## 7. Safety rules

1. Start-up never moves the dog. A boot, a link loss, a configuration change and a registry change set DISARMED.
2. Only a person at the dog can arm it: a double press of the button, or `robot.enable` over USB. The cloud cannot arm it.
3. The armed state ends after 10 minutes with no action.
4. Each press of the button stops the action and the speaker.
5. When the hub link goes down, the board stops the action and disarms.
6. Each action ends on the board, at a limit from the registry:
   - A hold command stops at `max_run_ms`, which is 1000 or less.
   - An episodic trick waits `natural_ms`, which is 10000 or less for the cloud.
   - A perpetual trick ends with its `stop_via` command at `max_run_ms`.
7. The board runs one action at a time. A second action gets BUSY.
8. The board keeps the last 32 action ids. A resent id does not move the dog again.
9. The cloud starts only verified commands with `cloud: true`.
10. The board never sends IR while it captures IR.
11. Each result has `transmitted`. `observed` is null, because the board has no camera.

The heartbeat watchdog of the IR bridge is not necessary on this path. The hub sends no leases over the internet, and each action ends on the board.

## 8. Button and LED

| Input | Result |
| --- | --- |
| A press | Stops the action and the speaker. Sends `voice.cancel`. |
| A hold of 300 ms or more | Push-to-talk. The audio starts at the press and ends at the release, or after 10 s. |
| Two presses in 400 ms | Arms or disarms the dog. |

A hold of less than 300 ms sends no audio. The LED is yellow without a hub link, red when DISARMED, green when armed and blue during push-to-talk.

## 9. Audio

- Microphone: 16 kHz, 16-bit mono PCM, in chunks of 100 ms. The hub joins the chunks, then runs the voice loop of `cloud.md`.
- Speaker: 24 kHz, 16-bit mono PCM from the OpenAI TTS. The board has a ring buffer of 1.5 s.
- `link.hello` reports `play_buffer_ms: 1000`. The hub sends parts of 250 ms and keeps 750 ms or less in front of the speaker.
- The extra 500 ms in the ring buffer absorbs network delay.
- `audio.play` gets BUSY when the part does not fit, or during push-to-talk.
- Microphone gain and speaker volume are firmware constants. Adjust them at the first test.

## 10. Hub changes

- Accept `voice.start`, `voice.chunk` and `voice.end`. Keep `voice.utterance` for the Pi path.
- Size the `audio.play` parts from `play_buffer_ms` in `link.hello`.
- Change the NOT_ARMED text: "ask the owner to arm the dog at the dog".
- When `caps.look` is false, `look` and `go_to_marker` say that the dog has no camera.

## 11. Setup

The owner does these steps when the parts arrive:

1. Push the Unit IR cable into the Grove port of the Atom Echo.
2. Connect the Atom Echo to the Mac with the USB-C data cable.
3. Put the Wi-Fi name and password in the password manager.

The implementer then flashes the board, sets the Wi-Fi and the hub address, and runs the conformance tests. Then the owner presses each of the 19 remote buttons when the capture tool asks.

Put the board 3 m or less from the dog, with a clear line to the front of the head.

## 12. Phases and issues

| Phase | Work | Issues | Who |
| --- | --- | --- | --- |
| 0 | Order the Atom Echo and the Unit IR | #19 (done) | Owner |
| 0 | Droplet, DNS, keys, Telegram bot | #21 to #24 | Owner |
| 3b | Board software and hub changes | #143 (#145 to #152) | Implementer |
| 4 | Server and Hermes Agent | #12 | Both |
| 5 | Board conformance, capture of 19 buttons, go/no-go | #13 | Both |
| 6 | Board on Wi-Fi and the hub, arm and stop rules | #14 | Both |
| 7 | Stop latency of each command, verified flags | #15 | Both |
| 8 | Voice end to end | #16 | Both |
| 9 | Navigation to a marker | #17 | Waits for #144 |
| 10 | Physical fault matrix | #18 | Both |

Go/no-go in phase 5: one trick works 9 times in 10 at the planned distance. Also find out if the center button only stops, or toggles between stop and play.

## 13. Risks

| Risk | Control |
| --- | --- |
| The Unit IR range is 5 m or less, with line of sight. | Put the board 3 m or less in front of the dog. If this fails, connect the board to the white signal wire of the receiver (section 1). |
| The center button toggles between stop and play. | Then it cannot be a `stop_via` command. Cloud tricks are then hold commands or bounded episodic tricks only. |
| Wi-Fi stops during an action. | The action ends on the board at its limit. The link loss disarms the dog. |
| NVS keeps the Wi-Fi password and DEVICE_TOKEN without encryption. | A person with the board can read them. When the board is lost, change DEVICE_TOKEN and the Wi-Fi password. |
| The board trusts only ISRG Root X1 and X2 (Let's Encrypt). | When Caddy uses a different issuer, update the firmware. |
