# Media

This document owns the camera, the markers, the pose, the audio rules, the LAN console and the gamepad controls. When this document and the code disagree, correct this document first. Then correct the code.

## 1. Processes

| Process | File | Job |
| --- | --- | --- |
| `mediad` | `pi/mediad.py` | Camera, ArUco markers, pose, microphone, speaker, LAN console. It is the only writer of the pose. |
| `padd` | `pi/padd.py` | Wired USB gamepad. |

Both processes are local clients of `robotd`. They move the dog only through `robotd` methods.

## 2. Camera

- The device is `[mediad] camera` in `dog.toml`. Use the persistent path in `/dev/v4l/by-id/`.
- mediad reads frames of 640 x 480 px at 15 frames each second.
- The capture buffer holds one frame. Thus mediad always has the newest frame.
- A thread reads the frames. Thus a slow camera cannot stop the bus or the console.
- mediad keeps the newest frame and its monotonic time. `media.snapshot` and the console use this frame.
- When the camera fails or is disconnected, mediad opens it again each 1 s.

## 3. Markers

| Item | Value | Configuration |
| --- | --- | --- |
| Dictionary | `DICT_4X4_50` | `[mediad] aruco_dict` |
| Marker on the dog | id 1 | `[mediad] robot_marker` and `goto.robot_marker` in the registry. The two values must be equal. |
| Goal markers | Each other id of the dictionary | None |

The pose of one marker is `[cx, cy, heading_rad]` in image pixels:

- `cx, cy` is the center of the marker: the mean of its four corners.
- `heading_rad` is the angle from the center to the midpoint of the top edge. The top edge goes from corner 0 to corner 1.
- Image y points down. Thus a positive angle is clockwise on the screen.
- No camera calibration is necessary.

### 3.1 Attach the marker to the dog

1. Print the markers upright, with a white margin of one cell or more on each side.
2. Mark the top edge of the marker with a pen.
3. Cut the marker from the sheet. Keep the white margin.
4. Put the marker flat on the back of the dog, with its top edge toward the nose.
5. Attach the marker with tape.

### 3.2 Rules

- Keep each marker and its white margin 5 px or more inside the image edge. The detector does not find a marker nearer to the edge.
- Thus keep the area polygon (`goto.area_px`) one marker width inside the image edge.
- The dog can cover a goal marker on the floor when it arrives. During a goto, robotd thus uses the last position where the camera saw the goal.

## 4. Pose

- For each frame, mediad sends `robot.observe {t_mono_ns, markers}` to `robotd.sock`. `t_mono_ns` is the monotonic time of the frame.
- The rate is the camera rate: 15 Hz.
- mediad also sends a frame with no markers. Then robotd knows that the camera works but does not see the dog.
- mediad never waits for robotd. When robotd starts again, mediad connects again in 0.5 s or less.
- `robot.state` gives the age of the last pose as `vision_age_ms`. robotd stops a goto when the pose is older than 2 s.

Targets: a pose rate of 10 Hz or more, and a `vision_age_ms` p95 of less than 200 ms.

## 5. Fake camera

`mediad --fake-camera` replaces the camera in the dev stack and in the tests.

- Each frame is a gray floor of 640 x 480 px.
- The fake draws the robot marker at the toy pose in `sim_pose.json`. The top edge of the marker points along the toy heading.
- The fake draws each goal marker of `fake_markers.json` upright. The file format is `{"2": [x, y]}`.
- mediad reads the two files in `DOG_RUN_DIR` for each frame. `Stack.set_goal_markers` in `sim/devstack.py` writes `fake_markers.json`.
- Each fake marker is 36 px, with a white margin of 6 px.
- Then the real detection runs on the drawn frame. Thus the tests use the same detection path as the real camera.

## 6. Audio

mediad uses `arecord` and `aplay` subprocesses. Set the ALSA devices in `[mediad] mic` and `[mediad] speaker`. Get the device names from `arecord -L` and `aplay -L`.

| Rule | Value |
| --- | --- |
| Microphone format | 16000 Hz, 16-bit signed little-endian, mono |
| Shortest utterance | 300 ms. mediad discards a shorter utterance. |
| Longest utterance | 10 s. At 10 s, mediad stops the microphone and sends the utterance. |
| Speaker format | 16-bit signed little-endian, mono, 8000 Hz to 48000 Hz (default 24000 Hz) |
| Longest play item | 30 s |
| Play queue | 16 items or less. mediad plays the items in sequence. |

When gamepad A goes down, padd sends `audio.ptt {down: true}`. Then mediad does these steps:

1. It stops `aplay` and clears the play queue.
2. It sends `media.bargein {}` to each subscriber.
3. It starts `arecord`.

When A goes up, padd sends `audio.ptt {down: false}`. mediad stops `arecord` and sends `media.utterance {id, rate, pcm16_b64}` to each subscriber. Each utterance has a new `id`.

- `audio.stop` stops `aplay` and clears the play queue.
- While A is down, mediad refuses `audio.play` with BUSY. Thus the speaker cannot play into the microphone.
- mediad records audio only while a person holds A.

`mediad --fake-audio` uses no subprocesses:

- An utterance is a quiet 440 Hz tone. Its length is the time that the person held A.
- mediad adds each play item to `audio_out.pcm` in `DOG_RUN_DIR`.
- mediad writes one line to `audio_out.log` for each event: `play`, `end` or `cut`. Each line starts with the monotonic time in ns.
- The fake takes the real play time of each item. Thus the tests can measure the barge-in time.

### 6.1 mediad methods

| Method | Parameters and result | Cloud |
| --- | --- | --- |
| `media.snapshot` | `max_px` from 16 to 4096 (default 640). Returns `jpeg_b64` and `age_ms`. The long side of the image is `max_px` or less. | Yes |
| `media.subscribe` | Sends `media.utterance` and `media.bargein` to this connection until it closes. | Yes |
| `audio.play` | `pcm16_b64`, `rate` (default 24000). Returns `queued`. A full queue or a push-to-talk press gives BUSY. | Yes |
| `audio.stop` | Stops the speaker and clears the queue. | Yes |
| `audio.ptt` | `down` (bool). From `padd` only. | No |

The sockets are `mediad.sock` (group `dog`) and `mediad-cloud.sock` (group `doglink`). The line limit is 2 MiB. A 10 s utterance is approximately 430 KiB of base64 text.

## 7. LAN console

The LAN console is the local interface of the owner. It is a small HTTP server in mediad. It calls robotd through `robotd.sock`, like each other local client.

| Item | Value |
| --- | --- |
| Address | `[mediad] console_bind` and `console_port`. `config/dog.toml` has 0.0.0.0 and 8080. Without these keys, mediad uses 127.0.0.1 and 8080. |
| Token | `DOG_CONSOLE_TOKEN` from `/etc/dog/console.env`. Without a token, the console is off. |

| Endpoint | Job |
| --- | --- |
| `GET /?token=<token>` | Sets the cookie and returns the page. The page removes the token from the address bar. |
| `GET /` | The page: STOP, Arm, Disarm, the camera image (two times each second), the state, and one button for each verified trick. |
| `GET /snapshot.jpg` | The newest camera frame as a JPEG. |
| `GET /state` | `robot.state` and `frame_age_ms`, as JSON. |
| `POST /arm` | `robot.enable`. This is a local arm path, like gamepad Start. |
| `POST /disarm` | `robot.disarm` |
| `POST /stop` | `robot.stop` |
| `POST /do` | Body `{"trick": "<name>"}`. Sends `robot.do` with a new action id. |

Access rules:

1. Each endpoint needs the token, in an `Authorization: Bearer <token>` header or in the cookie.
2. `GET /?token=<token>` sets the cookie `dog_console` with `HttpOnly` and `SameSite=Strict`.
3. The browser does not send this cookie when a link in a different app opens `/`. For such a link, use the URL with the token.
4. A wrong or absent token gets 401.
5. A POST with an `Origin` header from a different origin gets 403.
6. mediad compares tokens in constant time.

The console uses HTTP, not HTTPS. The token goes across the LAN as clear text. Use the console only on a trusted LAN.

### 7.1 Open the console on a phone

1. Read the token in `/etc/dog/console.env` on the Pi.
2. Connect the phone to the LAN of the Pi.
3. Open `http://robot-pi.local:8080/?token=<token>` on the phone.
4. Make sure that the page shows the camera image.

## 8. Gamepad

padd uses the first input device with two stick axes and the A, B and Start buttons. The reference pad is a Logitech F310 with its back switch at X (XInput). 8BitDo pads in XInput mode also work. After a disconnection, padd tries to find the pad again each 1 s.

| Control | Action | Message |
| --- | --- | --- |
| Left stick | Drive | `robot.move {vx, vyaw}` to robotd, 20 times each second while the stick is out of the dead band. One zero message when the stick goes back to the center. |
| Start | Arm | `robot.enable` |
| B | Stop | `robot.stop` |
| Select, held for 1 s | Disarm | `robot.disarm` |
| A, held | Push-to-talk | `audio.ptt {down: true}` to mediad, then `audio.ptt {down: false}` when A goes up. |
| X, Y, LB, RB | Trick | `robot.do {trick, id}` with the trick from `[padd.bindings]`. Each press has a new id. |

Stick rules:

- `vx` is forward motion. The stick fully up gives 1.
- `vyaw` is a left turn. The stick fully left gives 1.
- padd scales each axis to -1 to 1 with the minimum and the maximum that the device gives.
- The dead band of padd is 0.15. robotd uses its own dead band of 0.5 to select one drive direction.

Bindings are empty by default. Example:

~~~toml
[padd.bindings]
X = "bark"
~~~

padd does not start when a key is not X, Y, LB or RB.

Safety rules:

- padd sends each request one time. It never sends a `robot.do` again after an error.
- When padd stops, the robotd deadman stops the stick motion after 500 ms.
- Stick input has priority over a cloud action. robotd cancels the cloud action.
- B is the local stop. It does not need speech, the cloud or the hub.

The xpad driver gives these Linux codes: A 304, B 305, X 307, Y 308, LB 310, RB 311, Select 314, Start 315. The stick axes are `ABS_X` (0) and `ABS_Y` (1).

## 9. Measured values

| Value | Target | Dev stack (fake camera, fake audio) |
| --- | --- | --- |
| Pose rate | 10 Hz or more | 15.0 Hz |
| `vision_age_ms` p95 | Less than 200 ms | 65 ms |
| Push-to-talk cut of the fake speaker | 100 ms or less | p50 0.13 ms, maximum 0.17 ms, 10 trials |
| goto with the fake camera | 9 of 10 goals | 10 of 10 goals |
| Heading error of a drawn marker | Less than 5° | 0.7° or less |

The real camera, microphone, speaker and gamepad are not measured yet. Phase 7 and phase 8 measure them.
