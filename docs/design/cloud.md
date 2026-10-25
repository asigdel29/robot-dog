# Cloud link, robot tools and voice

This document owns the link between `linkd` and the hub, the robot tools, the voice loop, the time limits, the daily caps and the tokens. When this document and the code disagree, correct this document first. Then correct the code.

The code is in `pi/linkd.py`, `hub/hub.py` and `sim/stub_hermes.py`. The tests are in `tests/test_link.py`.

## 1. Parts

| Part | Where | Job |
| --- | --- | --- |
| `linkd` | Pi, user `doglink` | The only process on the Pi with internet access. It holds the WebSocket link to the hub. |
| Caddy | Droplet | TLS. It makes only `/device` and `/healthz` public. All other paths get 404. |
| `hub.py` | Droplet, `127.0.0.1:8000` | Device link, robot tools and voice loop, in one process. |
| Hermes Agent | Droplet, `127.0.0.1:8642` | The agent. It calls the robot tools and answers the voice turns. |
| `stub_hermes.py` | Laptop, CI | A stand-in for Hermes and for the OpenAI speech endpoints, for the tests. |

## 2. Link from linkd to the hub

### 2.1 Connection

- linkd reads `DOG_HUB_URL`, `DOG_DEVICE_TOKEN` and `DOG_DEVICE` (default `dog-01`) from `/etc/dog/link.env` (mode 0600).
- The URL must start with `wss://`. For tests, linkd also accepts `ws://` to `127.0.0.1`, `localhost` or `::1`.
- linkd does not start without a valid URL and a token.
- linkd sends the header `Authorization: Bearer <DOG_DEVICE_TOKEN>`. The token is never in the URL.
- The `websockets` DEBUG log shows the handshake headers, which include the token. Thus linkd keeps that log at INFO.
- Each message is one JSON-RPC 2.0 object in one text frame. The frame limit is 2 MiB.
- Each side sends a ping each 5 s. A side closes the link when no pong arrives in 5 s.
- linkd waits 1 s for the close handshake. Thus linkd drops a silent hub in 11 s or less. Measured: 11.0 s with a frozen hub process.
- The first frame from linkd is `link.hello {api, version, device}`. `version` is the name of the release directory.

The hub does these checks on the link:

- The first frame in 10 s must be a text frame with a `link.hello` for `dog-01`. Otherwise the hub closes the link with code 1008.
- A different `api` value is logged. It is not refused.
- The hub has one device. A new link replaces the old link. The hub closes the old link with code 4000, and the open requests on it fail.
- The hub drops a frame that is binary, that is not a JSON object, or that has `NaN` or `Infinity`.
- The hub ignores a reply whose `id` is not an integer.
- The hub accepts `voice.utterance` and `voice.cancel` only from the current link.

### 2.2 Requests from the hub

The hub sends only requests. Each request has a top-level `sent_at` field: the Unix time in milliseconds. linkd records the time when the frame arrives. Then it does these checks in this sequence:

1. The method must be in `CLOUD_METHODS` and must not be `media.subscribe`. Otherwise linkd returns METHOD_NOT_FOUND.
2. `sent_at` must be a finite number. Otherwise linkd returns STALE.
3. The Pi clock must have NTP sync. Otherwise linkd returns STALE.
4. The difference between the arrival time and `sent_at` must be 1500 ms or less, in the past or in the future. Otherwise linkd returns STALE.

linkd does not do checks 2 to 4 for `robot.stop`, `robot.disarm` and `audio.stop`. These methods only decrease motion or sound, so linkd always sends them.

A background task reads the NTP state each 30 s with `timedatectl show -p NTPSynchronized --value`. The check never blocks a request. When the check fails or does not finish in 2 s, linkd keeps the last value and logs it. Until the first answer, the value is "not synced". The flag `--assume-synced` skips the check. Use it only for tests and dev work.

linkd removes `sent_at` and sends the method and its `params` to a daemon:

| Method | Socket |
| --- | --- |
| `robot.*` | `robotd-cloud.sock` |
| `media.*`, `audio.*` | `mediad-cloud.sock` |

- Each request runs in its own task. Replies are matched by `id`. Thus a stop can overtake a goto that is in progress.
- linkd waits 40 s for a daemon reply. Then it returns TIMEOUT.
- When linkd cannot connect to a daemon socket, it returns UNAVAILABLE.
- When the local bus cannot carry a value, for example `1e999` in `params`, linkd returns INVALID_PARAMS.
- linkd ignores a frame from the hub that is not a JSON object, that has `NaN` or `Infinity`, or that has no `id`.

### 2.3 Notifications from the Pi

linkd keeps one `media.subscribe` open on `mediad-cloud.sock`. When mediad restarts, linkd opens the subscription again. It tries each 1 s.

| From mediad | To the hub | Parameters |
| --- | --- | --- |
| `media.utterance` | `voice.utterance` | `id`, `rate`, `pcm16_b64`, without change |
| `media.bargein` | `voice.cancel` | none |

### 2.4 Link loss

When the link closes, or when linkd gets SIGTERM, linkd does these steps:

1. Call `robot.disarm` on `robotd-cloud.sock`.
2. Close the connections to robotd and mediad. robotd then cancels the actions of those connections.
3. Wait 1 s. Then connect again.

After each attempt, linkd doubles the wait, up to 30 s. Only a link that stayed open for more than 10 s sets the wait to 1 s again. Thus a hub that closes each link at once does not cause a disarm each second. A reconnect never arms the dog, because `robot.enable` is not a cloud method. Only a person at the Pi can arm the dog.
