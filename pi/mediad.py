"""mediad: camera, ArUco pose, microphone, speaker and the LAN console of the robot dog.

It is the only writer of the pose: it sends the markers of each camera frame to
robotd as robot.observe. It records push-to-talk audio with arecord, plays queued
replies with aplay and serves the owner console. docs/design/media.md owns the rules.
"""

import argparse
import asyncio
import base64
import contextlib
import functools
import hashlib
import hmac
import json
import logging
import math
import os
import sys
import threading
import time
import tomllib
import urllib.parse
import uuid
from http import HTTPStatus

import cv2
import numpy as np

import dogproto
from dogproto import RpcError

log = logging.getLogger("mediad")

CAMERA_FPS = 15
FRAME_W, FRAME_H = 640, 480
CAMERA_RETRY_S = 1.0
ROBOTD_RETRY_S = 0.5
FAKE_ROBOT_MARKER = 1  # goto.robot_marker of the dev stack registry
FAKE_MARKER_PX = 36  # 6 px for each cell of a 4x4 marker with its border
FAKE_MARGIN_PX = 6  # white quiet zone around each fake marker
FAKE_FLOOR_GRAY = 150
JPEG_QUALITY = 80
REC_RATE = 16000
MIN_UTTERANCE_MS = 300
MAX_UTTERANCE_S = 10
MAX_PLAY_S = 30
PLAY_RATES = (8000, 48000)
PLAY_QUEUE_MAX = 16
SNAPSHOT_PX = (16, 4096)
CONSOLE_COOKIE = "dog_console"
CONSOLE_MIN_TOKEN_CHARS = 32
CONSOLE_MAX_CONNECTIONS = 16
#: Console buttons that map to one robotd request without parameters.
CONSOLE_BUTTONS = {"/arm": "robot.enable", "/disarm": "robot.disarm", "/stop": "robot.stop"}
HTTP_HEAD_LIMIT = 8192
HTTP_BODY_LIMIT = 1024
HTTP_READ_S = 3.0
TRICK_WAIT_S = 35.0


# ---------------------------------------------------------------- vision


def require_aruco_detector():
    """Exit with a clear message on OpenCV older than 4.7, which has no cv2.aruco.ArucoDetector."""
    if not hasattr(getattr(cv2, "aruco", None), "ArucoDetector"):
        sys.exit(f"mediad needs OpenCV 4.7 or later (cv2.aruco.ArucoDetector); this is OpenCV {cv2.__version__}")


def aruco_dict():
    """Return the ArUco dictionary of the printed markers."""
    return cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


def make_detector():
    """Return detect(bgr_frame) -> {id: [cx, cy, heading_rad]}; real and fake frames use this one path.

    The heading is the angle from the marker center to the midpoint of its top
    edge (corners 0 and 1), with image y down.
    """
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    detector = cv2.aruco.ArucoDetector(aruco_dict(), params)

    def detect(frame):
        """Return the marker poses of one frame in image pixels."""
        corners, ids, _ = detector.detectMarkers(frame)
        poses = {}
        for quad, marker_id in zip(corners, [] if ids is None else ids.flatten()):
            points = quad.reshape(4, 2)
            cx, cy = points.mean(axis=0)
            tx, ty = (points[0] + points[1]) / 2
            poses[str(marker_id)] = [round(float(cx), 1), round(float(cy), 1), round(math.atan2(ty - cy, tx - cx), 4)]
        return poses

    return detect


@functools.cache
def marker_tile(marker_id):
    """Return one marker as a BGR image with a white quiet zone, top edge up."""
    marker = cv2.aruco.generateImageMarker(aruco_dict(), marker_id, FAKE_MARKER_PX)
    tile = cv2.copyMakeBorder(marker, *[FAKE_MARGIN_PX] * 4, cv2.BORDER_CONSTANT, value=255)
    return cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)


def draw_marker(frame, tile, cx, cy, heading):
    """Paint tile into frame centered at (cx, cy), turned so its top edge points along heading."""
    turn = heading + math.pi / 2  # an upright tile has its top edge at -pi/2
    cos, sin = math.cos(turn), math.sin(turn)
    half = (tile.shape[0] - 1) / 2
    matrix = np.float32([[cos, -sin, cx - cos * half + sin * half],
                         [sin, cos, cy - sin * half - cos * half]])
    cv2.warpAffine(tile, matrix, (frame.shape[1], frame.shape[0]), dst=frame,
                   flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_TRANSPARENT)


def read_json(name):
    """Return the JSON value of a file in DOG_RUN_DIR, or None when the file is absent or partial."""
    try:
        with open(os.path.join(dogproto.run_dir(), name)) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


class FakeCamera:
    """Frame source for dev and tests with the read/release interface of cv2.VideoCapture.

    Each frame is a floor with the upright goal markers of fake_markers.json
    ({"2": [x, y]}) and the robot marker at the toy pose of sim_pose.json.
    """

    def __init__(self):
        self._next = time.monotonic()

    def read(self):
        """Wait for the next frame time and return (True, frame)."""
        self._next = max(self._next + 1 / CAMERA_FPS, time.monotonic())
        time.sleep(max(0.0, self._next - time.monotonic()))
        frame = np.full((FRAME_H, FRAME_W, 3), FAKE_FLOOR_GRAY, np.uint8)
        for marker_id, (x, y) in (read_json("fake_markers.json") or {}).items():
            draw_marker(frame, marker_tile(int(marker_id)), x, y, -math.pi / 2)
        pose = read_json("sim_pose.json")
        if pose:
            draw_marker(frame, marker_tile(FAKE_ROBOT_MARKER), pose["x"], pose["y"], pose["heading"])
        return True, frame

    def release(self):
        """Do nothing; a fake camera holds no device."""


def open_camera(path):
    """Open a V4L2 camera at 640 x 480 and 15 fps with a one-frame buffer, or return None."""
    if not os.path.exists(path):  # unplugged: no OpenCV message each retry
        return None
    capture = cv2.VideoCapture(path, cv2.CAP_V4L2)
    if not capture.isOpened():
        return None
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    capture.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # the newest frame, not a queue of old ones
    return capture


def capture_loop(open_source, deliver):
    """Read frames forever (a thread): stamp, detect and deliver each one; reopen a failed source each 1 s."""
    detect = make_detector()
    source = None
    warned = False
    while True:
        try:
            if source is None:
                source = open_source()
            if source is not None:
                ok, frame = source.read()
                t_ns = time.monotonic_ns()
                if ok:
                    deliver(t_ns, frame, detect(frame))
                    warned = False
                    continue
            if not warned:
                log.warning("camera not available; retry each %.0f s", CAMERA_RETRY_S)
                warned = True
        except Exception:
            log.exception("camera loop failed")
        if source is not None:
            source.release()
            source = None
        time.sleep(CAMERA_RETRY_S)


def encode_jpeg(frame, max_px):
    """Return frame as JPEG bytes, scaled down so that its long side is at most max_px."""
    height, width = frame.shape[:2]
    scale = max_px / max(width, height)
    if scale < 1:
        frame = cv2.resize(frame, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])[1].tobytes()


def audio_log(event):
    """Append one fake speaker event with its monotonic time in ns to DOG_RUN_DIR/audio_out.log."""
    with open(os.path.join(dogproto.run_dir(), "audio_out.log"), "a") as handle:
        handle.write(f"{time.monotonic_ns()} {event}\n")


# ---------------------------------------------------------------- daemon


class Mediad:
    """All mediad state: the newest frame, the robotd link, subscribers, the play queue and push-to-talk."""

    def __init__(self, fake_audio, speaker, mic):
        self.fake_audio = fake_audio
        self.speaker = speaker
        self.mic = mic
        self.frame = None  # (t_mono_ns, BGR image)
        self.robotd = None
        self._subscribers = set()
        self._queue = asyncio.Queue()
        self._playing = None
        self._ptt_owner = None  # the connection that holds push-to-talk down
        self._ptt_watched = set()  # connections with a close callback for push-to-talk
        self._ptt_up = None
        self._recording = None

    def handlers(self):
        """Return the method table for dogproto.serve."""
        return {"media.snapshot": self.h_snapshot, "media.subscribe": self.h_subscribe,
                "audio.play": self.h_play, "audio.stop": self.h_stop, "audio.ptt": self.h_ptt}

    def on_frame(self, t_ns, frame, markers):
        """Keep the newest frame and send its marker poses to robotd; never waits on robotd."""
        self.frame = (t_ns, frame)
        if self.robotd is not None:
            self.robotd.notify("robot.observe", {"t_mono_ns": t_ns, "markers": markers})

    def frame_age_ms(self):
        """Return the age of the newest frame in ms, or None before the first frame."""
        return None if self.frame is None else (time.monotonic_ns() - self.frame[0]) // 1_000_000

    async def snapshot(self, max_px):
        """Return (JPEG bytes, age_ms) of the newest frame."""
        if self.frame is None:
            raise RpcError(dogproto.UNAVAILABLE, "no camera frame yet")
        age_ms = self.frame_age_ms()
        return await asyncio.to_thread(encode_jpeg, self.frame[1], max_px), age_ms

    def _notify(self, method, params):
        """Send one notification to each media.subscribe connection."""
        for conn in list(self._subscribers):
            conn.notify(method, params)

    async def player(self):
        """Play the queued items one at a time; cut() cancels the current one."""
        while True:
            pcm, rate = await self._queue.get()
            self._playing = asyncio.ensure_future(self._play_one(pcm, rate))
            await asyncio.wait({self._playing})
            if not self._playing.cancelled() and self._playing.exception() is not None:
                log.error("playback failed: %s", self._playing.exception())
            self._playing = None

    def cut(self):
        """Silence the speaker now and drop the queue."""
        while not self._queue.empty():
            self._queue.get_nowait()
        if self._playing is not None:
            self._playing.cancel()

    async def _play_one(self, pcm, rate):
        """Play one PCM16 mono item to its end; a cancel stops it at once."""
        if self.fake_audio:
            audio_log("play")
            try:
                await asyncio.sleep(len(pcm) / 2 / rate)  # the real play time
            except asyncio.CancelledError:
                audio_log("cut")
                raise
            audio_log("end")
            return
        proc = await asyncio.create_subprocess_exec(
            "aplay", "-q", "-D", self.speaker, "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(rate),
            stdin=asyncio.subprocess.PIPE)
        try:
            proc.stdin.write(pcm)
            with contextlib.suppress(ConnectionError):
                await proc.stdin.drain()
                proc.stdin.close()
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def _record(self, ptt_up):
        """Record until ptt_up or the 10 s cap; send the utterance unless it is shorter than 300 ms."""
        try:
            pcm = await (self._fake_capture(ptt_up) if self.fake_audio else self._arecord(ptt_up))
        except OSError as exc:
            log.error("recording failed: %s", exc)
            return
        ms = len(pcm) * 1000 // (2 * REC_RATE)
        if ms < MIN_UTTERANCE_MS:
            log.info("utterance of %d ms dropped", ms)
            return
        self._notify("media.utterance", {"id": uuid.uuid4().hex, "rate": REC_RATE,
                                         "pcm16_b64": base64.b64encode(pcm).decode()})

    async def _fake_capture(self, ptt_up):
        """Return silence as long as the press, capped at 10 s."""
        start = time.monotonic()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ptt_up.wait(), MAX_UTTERANCE_S)
        return bytes(2 * int(min(time.monotonic() - start, MAX_UTTERANCE_S) * REC_RATE))

    async def _arecord(self, ptt_up):
        """Return the PCM that arecord gives until ptt_up or the 10 s cap."""
        proc = await asyncio.create_subprocess_exec(
            "arecord", "-q", "-D", self.mic, "-t", "raw", "-f", "S16_LE", "-c", "1", "-r", str(REC_RATE),
            stdout=asyncio.subprocess.PIPE)
        reader = asyncio.ensure_future(proc.stdout.readexactly(int(MAX_UTTERANCE_S * REC_RATE * 2)))
        waiter = asyncio.ensure_future(ptt_up.wait())
        await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        try:
            return await reader
        except asyncio.IncompleteReadError as exc:
            return exc.partial

    async def h_snapshot(self, params, conn):
        """media.snapshot: return the newest frame as a JPEG with its age."""
        max_px = params.get("max_px", FRAME_W)
        if not SNAPSHOT_PX[0] <= max_px <= SNAPSHOT_PX[1]:
            raise RpcError(dogproto.INVALID_PARAMS, f"max_px must be {SNAPSHOT_PX[0]}..{SNAPSHOT_PX[1]}")
        jpeg, age_ms = await self.snapshot(max_px)
        return {"jpeg_b64": base64.b64encode(jpeg).decode(), "age_ms": age_ms}

    async def h_subscribe(self, params, conn):
        """media.subscribe: send media.utterance and media.bargein to this connection until it closes."""
        self._subscribers.add(conn)
        conn.on_close.append(lambda: self._subscribers.discard(conn))
        return {}

    async def h_play(self, params, conn):
        """audio.play: queue one PCM16 mono item of 30 s or less; refused while push-to-talk is down."""
        rate = params.get("rate", 24000)
        if not PLAY_RATES[0] <= rate <= PLAY_RATES[1]:
            raise RpcError(dogproto.INVALID_PARAMS, f"rate must be {PLAY_RATES[0]}..{PLAY_RATES[1]}")
        try:
            pcm = base64.b64decode(params["pcm16_b64"], validate=True)
        except ValueError as exc:  # binascii.Error, or a character outside ASCII
            raise RpcError(dogproto.INVALID_PARAMS, "pcm16_b64 is not base64") from exc
        if not pcm or len(pcm) % 2:
            raise RpcError(dogproto.INVALID_PARAMS, "pcm16_b64 must hold whole 16-bit samples")
        if len(pcm) / 2 / rate > MAX_PLAY_S:
            raise RpcError(dogproto.INVALID_PARAMS, f"an item must be {MAX_PLAY_S} s or less")
        if self._queue.qsize() >= PLAY_QUEUE_MAX:
            raise RpcError(dogproto.BUSY, "play queue full")
        if self._ptt_owner is not None:
            raise RpcError(dogproto.BUSY, "push-to-talk is down")  # never play into the microphone
        self._queue.put_nowait((pcm, rate))
        return {"queued": self._queue.qsize()}

    async def h_stop(self, params, conn):
        """audio.stop: stop the speaker and clear the queue."""
        self.cut()
        return {}

    async def h_ptt(self, params, conn):
        """audio.ptt: down cuts the speaker, sends media.bargein and records; the press ends on up or close of conn."""
        if not params["down"]:
            self._release_ptt()
            return {}
        self.cut()
        self._notify("media.bargein", {})
        if self._ptt_owner is None:
            self._ptt_owner = conn
            if conn not in self._ptt_watched:
                self._ptt_watched.add(conn)
                conn.on_close.append(lambda: self._ptt_closed(conn))
            if self._recording is None or self._recording.done():
                self._ptt_up = asyncio.Event()
                self._recording = asyncio.ensure_future(self._record(self._ptt_up))
        return {}

    def _release_ptt(self):
        """End the press; audio.play works again."""
        self._ptt_owner = None
        if self._ptt_up is not None:
            self._ptt_up.set()

    def _ptt_closed(self, conn):
        """Release push-to-talk when the connection that pressed closes, so a dead padd cannot keep the mic open."""
        self._ptt_watched.discard(conn)
        if self._ptt_owner is conn:
            self._release_ptt()


# ---------------------------------------------------------------- LAN console


CONSOLE_PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dog console</title>
<style>
body{margin:0 auto;padding:16px;max-width:640px;font-family:system-ui,sans-serif;background:#111;color:#eee}
button{width:100%;margin:4px 0;padding:14px;border:0;border-radius:10px;font-size:1.2rem;background:#444;color:#fff}
#stop{padding:28px;font-size:2rem;background:#d11}
img{width:100%;aspect-ratio:4/3;background:#333}
pre{white-space:pre-wrap}
</style></head><body>
<button id="stop">STOP</button><button id="arm">Arm</button><button id="disarm">Disarm</button>
<img id="cam" alt="Camera image"><pre id="state">Loading</pre><div id="tricks"></div><pre id="reply"></pre>
<script>
const $ = id => document.getElementById(id);
history.replaceState(null, '', '/');
async function post(path, body) {
  $('reply').textContent = path + ' ...';
  try {
    const r = await fetch(path, {method: 'POST', body: JSON.stringify(body || {})});
    $('reply').textContent = path + ': ' + await r.text();
  } catch (e) { $('reply').textContent = path + ': ' + e; }
  poll();
}
for (const id of ['stop', 'arm', 'disarm']) $(id).onclick = () => post('/' + id);
let trickNames;
async function poll() {
  try {
    const s = await (await fetch('/state')).json();
    $('state').textContent = s.error ? 'robotd: ' + s.error : `armed: ${s.armed}  health: ${s.health}  fault: ${s.fault}
esp32: ${s.esp}  busy: ${JSON.stringify(s.busy)}
vision age: ${s.vision_age_ms} ms  frame age: ${s.frame_age_ms} ms`;
    const names = (s.tricks || []).map(t => t.name);
    if (names.join() !== trickNames) {  // a new button under a finger would lose the tap
      trickNames = names.join();
      $('tricks').replaceChildren(...names.map(name => {
        const b = document.createElement('button');
        b.textContent = name;
        b.onclick = () => post('/do', {trick: name});
        return b;
      }));
    }
  } catch (e) { $('state').textContent = String(e); }
}
function nextFrame() { setTimeout(() => { $('cam').src = '/snapshot.jpg?t=' + Date.now(); }, 500); }
$('cam').onload = $('cam').onerror = nextFrame;
nextFrame();
poll();
setInterval(poll, 1000);
</script></body></html>
"""


def http_response(status, content_type, body, extra=()):
    """Return the bytes of one HTTP/1.1 response that closes the connection."""
    head = [f"HTTP/1.1 {status} {HTTPStatus(status).phrase}", f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}", "Cache-Control: no-store", "X-Content-Type-Options: nosniff",
            "X-Frame-Options: DENY", "Connection: close", *extra]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


def json_response(status, value):
    """Return an HTTP response with a JSON body."""
    return http_response(status, "application/json", json.dumps(value).encode())


async def read_request(reader):
    """Return (method, path, query, headers, body) of one request; raise ValueError when it is malformed."""
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, version = lines[0].split(" ")
    if not version.startswith("HTTP/1."):
        raise ValueError("not HTTP/1")
    headers = {}
    for line in lines[1:]:
        if line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
    if "transfer-encoding" in headers:
        raise ValueError("chunked bodies are not supported")
    length = int(headers.get("content-length", "0"))
    if not 0 <= length <= HTTP_BODY_LIMIT:
        raise ValueError("body too large")
    body = await reader.readexactly(length) if length else b""
    url = urllib.parse.urlsplit(target)
    return method, url.path, urllib.parse.parse_qs(url.query), headers, body


class Console:
    """The LAN console: the page, the snapshot, the state, and the arm, disarm, stop and trick buttons.

    Each endpoint needs the token in a Bearer header, or the HttpOnly SameSite=Strict
    cookie that GET /?token=... sets. The cookie holds an HMAC of the token, not the token.
    """

    def __init__(self, media, token):
        self.media = media
        self._token = token.encode()
        self._cookie = hmac.new(self._token, b"cookie", hashlib.sha256).hexdigest().encode()
        self._open = 0

    def _token_ok(self, candidate):
        """Return True when candidate is the console token; constant time."""
        return hmac.compare_digest(candidate.encode(), self._token)

    def _authorized(self, headers):
        """Return True when the request has the token in a Bearer header or the cookie value in the cookie."""
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer ") and self._token_ok(auth[len("Bearer "):]):
            return True
        # A plain split: SimpleCookie drops every cookie after one that it cannot parse.
        cookies = dict(part.strip().partition("=")[::2] for part in headers.get("cookie", "").split(";"))
        return hmac.compare_digest(cookies.get(CONSOLE_COOKIE, "").encode(), self._cookie)

    async def handle(self, reader, writer):
        """Serve one HTTP request on one connection, then close it; refuse more than 16 open connections."""
        if self._open >= CONSOLE_MAX_CONNECTIONS:
            writer.close()
            return
        self._open += 1
        try:
            try:
                request = await asyncio.wait_for(read_request(reader), HTTP_READ_S)
            except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                response = http_response(400, "text/plain", b"bad request\n")
            else:
                try:
                    response = await self.route(*request)
                except Exception:
                    log.exception("console request failed")
                    response = http_response(500, "text/plain", b"internal error\n")
            writer.write(response)
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            self._open -= 1
            writer.close()

    async def route(self, method, path, query, headers, body):
        """Return the response bytes for one parsed request."""
        if method == "GET" and path == "/" and "token" in query:
            if not self._token_ok(query["token"][0]):
                return http_response(401, "text/plain", b"wrong console token\n")
            # The page itself, not a redirect: a browser does not send a Strict cookie on a
            # redirect that another app started. The page removes the token from the address bar.
            cookie = f"Set-Cookie: {CONSOLE_COOKIE}={self._cookie.decode()}; HttpOnly; SameSite=Strict; Path=/"
            return http_response(200, "text/html; charset=utf-8", CONSOLE_PAGE, (cookie,))
        if not self._authorized(headers):
            return http_response(401, "text/plain", b"open /?token=<console token> first\n")
        origin = headers.get("origin")
        if method == "POST" and origin is not None and origin != f"http://{headers.get('host', '')}":
            return http_response(403, "text/plain", b"cross-origin request refused\n")
        if (method, path) == ("GET", "/"):
            return http_response(200, "text/html; charset=utf-8", CONSOLE_PAGE)
        if (method, path) == ("GET", "/snapshot.jpg"):
            try:
                jpeg, _ = await self.media.snapshot(FRAME_W)
            except RpcError as err:
                return http_response(503, "text/plain", err.message.encode())
            return http_response(200, "image/jpeg", jpeg)
        if (method, path) == ("GET", "/state"):
            _, state = await self._robot("robot.state")
            return json_response(200, {**state, "frame_age_ms": self.media.frame_age_ms()})
        if method == "POST" and path in CONSOLE_BUTTONS:
            return json_response(*await self._robot(CONSOLE_BUTTONS[path]))
        if (method, path) == ("POST", "/do"):
            try:
                params = {"trick": dogproto.decode(body)["trick"], "id": uuid.uuid4().hex}
            except (ValueError, TypeError, KeyError, RecursionError):
                return json_response(400, {"error": 'body must be {"trick": name}'})
            return json_response(*await self._robot("robot.do", params, TRICK_WAIT_S))
        return http_response(404, "text/plain", b"not found\n")

    async def _robot(self, method, params=None, timeout=5.0):
        """Call robotd on its local socket; return (HTTP status, JSON value)."""
        try:
            return 200, await dogproto.call_once(dogproto.sock_path("robotd"), method, params, timeout)
        except RpcError as err:
            return 409, {"error": err.message, "code": err.code}
        except (OSError, TimeoutError) as exc:
            return 503, {"error": f"robotd not reachable: {exc}"}


# ---------------------------------------------------------------- start-up


async def main(argv=None):
    """Start mediad: sockets, console, camera thread, robotd link and player; run until killed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.environ.get("DOG_CONFIG", "/etc/dog/dog.toml"))
    parser.add_argument("--fake-camera", action="store_true", help="draw markers from DOG_RUN_DIR/sim_pose.json")
    parser.add_argument("--fake-audio", action="store_true", help="log audio to DOG_RUN_DIR instead of aplay/arecord")
    parser.add_argument("--console-bind")
    parser.add_argument("--console-port", type=int)
    args = parser.parse_args(argv)
    require_aruco_detector()
    try:
        with open(args.config, "rb") as handle:
            cfg = tomllib.load(handle).get("mediad", {})
    except FileNotFoundError:
        cfg = {}
    media = Mediad(args.fake_audio, cfg.get("speaker", "default"), cfg.get("mic", "default"))
    os.makedirs(dogproto.run_dir(), exist_ok=True)
    await dogproto.serve(dogproto.sock_path("mediad"), media.handlers(), limit=dogproto.MEDIA_LIMIT, group="dog")
    await dogproto.serve(dogproto.sock_path("mediad", cloud=True), media.handlers(), cloud=True,
                         limit=dogproto.MEDIA_LIMIT, group="doglink")
    token = os.environ.get("DOG_CONSOLE_TOKEN", "")
    if len(token) >= CONSOLE_MIN_TOKEN_CHARS:
        bind = args.console_bind or cfg.get("console_bind", "127.0.0.1")
        port = args.console_port or cfg.get("console_port", 8080)
        await asyncio.start_server(Console(media, token).handle, bind, port, limit=HTTP_HEAD_LIMIT)
        log.info("console on http://%s:%d", bind, port)
    else:
        log.error("DOG_CONSOLE_TOKEN must have %d characters or more; the LAN console is off", CONSOLE_MIN_TOKEN_CHARS)
    open_source = FakeCamera if args.fake_camera else functools.partial(open_camera, cfg.get("camera", "/dev/video0"))
    deliver = functools.partial(asyncio.get_running_loop().call_soon_threadsafe, media.on_frame)
    threading.Thread(target=capture_loop, args=(open_source, deliver), daemon=True).start()
    log.info("mediad ready, api %d", dogproto.API_VERSION)
    # systemd stops aplay and arecord with mediad: the unit kills its whole control group.
    await asyncio.gather(media.player(), dogproto.keep_connected(
        dogproto.sock_path("robotd"), functools.partial(setattr, media, "robotd"), ROBOTD_RETRY_S))


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("DOG_LOG", "INFO"), format="%(name)s %(levelname)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(sys.argv[1:]))
