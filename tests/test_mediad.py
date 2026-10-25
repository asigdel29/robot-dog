"""Tests of mediad: marker math on drawn frames, snapshot, push-to-talk, barge-in, cloud rules and console auth.

The stack tests run mediad with --fake-camera and --fake-audio, so they need no
camera, microphone or speaker. The in-process tests put stub aplay and arecord
scripts on PATH, so the real subprocess paths run too. The barge-in latency is
printed so a run can go into docs/test-log.md.
"""

import asyncio
import base64
import hashlib
import hmac
import http.client
import math
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "sim"))

import dogproto  # noqa: E402
import mediad  # noqa: E402
from devstack import Stack, free_port  # noqa: E402

#: Stub aplay: records its pid and never reads stdin, like a long playback.
STUB_APLAY = """#!/bin/sh
echo $$ > "$STUB_DIR/aplay.pid"
exec sleep 30
"""

#: Stub arecord: records its pid and writes 16 kHz PCM16 at the real rate.
STUB_ARECORD = f"""#!{sys.executable}
import os, sys, time
with open(os.path.join(os.environ["STUB_DIR"], "arecord.pid"), "w") as handle:
    handle.write(str(os.getpid()))
while True:
    sys.stdout.buffer.write(b"\\x01\\x00" * 800)
    sys.stdout.buffer.flush()
    time.sleep(0.05)
"""


def floor():
    """Return an empty fake camera frame."""
    return np.full((mediad.FRAME_H, mediad.FRAME_W, 3), mediad.FAKE_FLOOR_GRAY, np.uint8)


def angle_diff_deg(a, b):
    """Return the absolute difference of two angles in degrees, wrapped to 0..180."""
    return abs(math.degrees(math.atan2(math.sin(a - b), math.cos(a - b))))


class MarkerMath(unittest.TestCase):
    """Center and heading from the real detector on drawn markers."""

    @classmethod
    def setUpClass(cls):
        cls.dictionary = mediad.aruco_dictionary("DICT_4X4_50")
        cls.detect = staticmethod(mediad.make_detector(cls.dictionary))

    def test_heading_within_5_degrees(self):
        for degrees in range(-180, 180, 15):
            heading = math.radians(degrees)
            frame = floor()
            mediad.draw_marker(frame, mediad.marker_tile(self.dictionary, 1), 300.5, 200.25, heading)
            cx, cy, found = self.detect(frame)["1"]
            self.assertLess(angle_diff_deg(found, heading), 5.0, f"heading {degrees} deg")
            self.assertLess(math.hypot(cx - 300.5, cy - 200.25), 1.0, f"center at {degrees} deg")

    def test_upright_marker_faces_up(self):
        frame = floor()
        mediad.draw_marker(frame, mediad.marker_tile(self.dictionary, 3), 320, 240, -math.pi / 2)
        self.assertLess(angle_diff_deg(self.detect(frame)["3"][2], -math.pi / 2), 2.0)

    def test_several_markers_and_none(self):
        self.assertEqual(self.detect(floor()), {})
        frame = floor()
        for marker_id, x in ((1, 100), (2, 320), (7, 540)):
            mediad.draw_marker(frame, mediad.marker_tile(self.dictionary, marker_id), x, 240, 0.3)
        self.assertEqual(sorted(self.detect(frame)), ["1", "2", "7"])

    def test_bad_dictionary_refused(self):
        with self.assertRaises(ValueError):
            mediad.aruco_dictionary("os")

    def test_old_opencv_refused(self):
        old = types.SimpleNamespace(__version__="4.6.0", aruco=types.SimpleNamespace())
        with mock.patch.object(mediad, "cv2", old), self.assertRaises(SystemExit) as ctx:
            mediad.require_aruco_detector()
        self.assertIn("4.7", str(ctx.exception.code))
        mediad.require_aruco_detector()


class FakeConn:
    """Stands in for a dogproto connection: keeps notifications and runs on_close callbacks on close()."""

    def __init__(self):
        self.cloud = False
        self.on_close = []
        self.notes = []

    def notify(self, method, params=None):
        """Keep one notification."""
        self.notes.append((method, params))

    def close(self):
        """Run the on_close callbacks, as dogproto does when a client goes away."""
        for callback in self.on_close:
            callback()


async def until(predicate, what, timeout=3.0):
    """Wait until predicate() is true; fail with what after timeout."""
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def gone(pid):
    """Return True when no process (not even a zombie) has this pid."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def read_pid(path):
    """Return the pid that a stub wrote, or None before it wrote it."""
    try:
        with open(path) as handle:
            return int(handle.read())
    except (OSError, ValueError):
        return None


class InProcess(unittest.IsolatedAsyncioTestCase):
    """Mediad handlers in this process: push-to-talk ownership, the cap and the real audio subprocess paths."""

    async def asyncSetUp(self):
        self.stub_dir = tempfile.mkdtemp()
        for name, body in (("aplay", STUB_APLAY), ("arecord", STUB_ARECORD)):
            path = os.path.join(self.stub_dir, name)
            with open(path, "w") as handle:
                handle.write(body)
            os.chmod(path, 0o755)
        env = {"PATH": self.stub_dir + os.pathsep + os.environ["PATH"], "STUB_DIR": self.stub_dir,
               "DOG_RUN_DIR": self.stub_dir}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)

    def media(self, fake_audio):
        """Return a mediad state object with one subscriber and the player running."""
        media = mediad.Mediad(fake_audio, "default", "default")
        self.sub = FakeConn()
        media._subscribers.add(self.sub)
        player = asyncio.ensure_future(media.player())
        self.addCleanup(player.cancel)
        return media

    async def play_refused(self, media):
        """Return True when audio.play gives BUSY."""
        pcm = base64.b64encode(bytes(4800)).decode()
        try:
            await media.h_play({"pcm16_b64": pcm}, FakeConn())
        except dogproto.RpcError as err:
            return err.code == dogproto.BUSY
        media.cut()
        return False

    def utterances(self):
        """Return the PCM byte counts of the utterances that the subscriber got."""
        return [len(base64.b64decode(p["pcm16_b64"])) for m, p in self.sub.notes if m == "media.utterance"]

    async def test_close_of_pressing_connection_releases_ptt(self):
        media = self.media(fake_audio=True)
        pad = FakeConn()
        await media.h_ptt({"down": True}, pad)
        self.assertTrue(await self.play_refused(media))
        await asyncio.sleep(0.4)
        pad.close()  # padd died or the pad was lost while A was down
        await until(lambda: self.utterances(), "utterance after the close")
        self.assertFalse(await self.play_refused(media))

    async def test_held_past_cap_sends_at_cap_and_stays_down(self):
        media = self.media(fake_audio=True)
        pad = FakeConn()
        with mock.patch.object(mediad, "MAX_UTTERANCE_S", 0.4):
            await media.h_ptt({"down": True}, pad)
            await until(lambda: self.utterances(), "utterance at the cap")
            self.assertAlmostEqual(self.utterances()[0] / 32000, 0.4, delta=0.05)
            self.assertTrue(await self.play_refused(media), "still down after the cap")
            await media.h_ptt({"down": False}, pad)
            self.assertFalse(await self.play_refused(media))
            self.assertEqual(len(self.utterances()), 1)

    async def test_cut_kills_aplay(self):
        media = self.media(fake_audio=False)
        await media.h_play({"pcm16_b64": base64.b64encode(bytes(48000)).decode()}, FakeConn())
        pid_path = os.path.join(self.stub_dir, "aplay.pid")
        await until(lambda: read_pid(pid_path), "aplay start")
        media.cut()
        await until(lambda: gone(read_pid(pid_path)), "aplay exit", timeout=2)
        await until(lambda: media._playing is None, "player idle")

    async def test_arecord_partial_read_and_kill(self):
        media = self.media(fake_audio=False)
        pad = FakeConn()
        await media.h_ptt({"down": True}, pad)
        pid_path = os.path.join(self.stub_dir, "arecord.pid")
        await until(lambda: read_pid(pid_path), "arecord start")
        await asyncio.sleep(0.5)
        await media.h_ptt({"down": False}, pad)
        await until(lambda: self.utterances(), "utterance after up")
        self.assertAlmostEqual(self.utterances()[0] / 32000, 0.5, delta=0.15)
        self.assertTrue(gone(read_pid(pid_path)))

    async def test_arecord_stops_at_cap(self):
        media = self.media(fake_audio=False)
        pad = FakeConn()
        with mock.patch.object(mediad, "MAX_UTTERANCE_S", 0.4):
            await media.h_ptt({"down": True}, pad)
            pid_path = os.path.join(self.stub_dir, "arecord.pid")
            await until(lambda: self.utterances(), "utterance at the cap")
            self.assertEqual(self.utterances(), [int(0.4 * 32000)])
            self.assertTrue(gone(read_pid(pid_path)), "arecord ends at the cap while A is down")
            self.assertTrue(await self.play_refused(media))
            await media.h_ptt({"down": False}, pad)

    async def test_console_error_gets_500(self):
        class BrokenMedia:
            async def snapshot(self, max_px):
                raise RuntimeError("broken camera path")

        token = "t" * 32
        server = await asyncio.start_server(mediad.Console(BrokenMedia(), token).handle, "127.0.0.1", 0)
        self.addCleanup(server.close)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        with self.assertLogs("mediad", "ERROR"):
            writer.write(f"GET /snapshot.jpg HTTP/1.1\r\nAuthorization: Bearer {token}\r\n\r\n".encode())
            status = await reader.readline()
        writer.close()
        self.assertIn(b" 500 ", status)


class MediaStack(unittest.TestCase):
    """mediad in the dev stack: snapshot, audio, cloud rules and the console."""

    @classmethod
    def setUpClass(cls):
        cls.stack = Stack(media=True)
        cls.stack.start()

    @classmethod
    def tearDownClass(cls):
        cls.stack.stop()

    def media(self, method, params=None, cloud=False):
        """Make one request to mediad of this stack."""
        return self.stack.call(method, params, cloud=cloud, service="mediad")

    # ---- snapshot

    def test_snapshot_is_jpeg_within_max_px(self):
        for max_px, long_side in ((None, 640), (640, 640), (200, 200)):
            reply = self.media("media.snapshot", {} if max_px is None else {"max_px": max_px}, cloud=True)
            jpeg = base64.b64decode(reply["jpeg_b64"])
            self.assertEqual(jpeg[:2], b"\xff\xd8")
            image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(max(image.shape[:2]), long_side)
            self.assertLess(reply["age_ms"], 500)
        with self.assertRaises(dogproto.RpcError) as ctx:
            self.media("media.snapshot", {"max_px": 0})
        self.assertEqual(ctx.exception.code, dogproto.INVALID_PARAMS)

    # ---- audio

    def subscribe_and_press(self, hold_s, wait_s):
        """Subscribe on the cloud socket like linkd, press ptt for hold_s, and return the notifications."""
        got = []

        async def scenario():
            sub = await dogproto.Client.connect(self.stack.sock("mediad", cloud=True),
                                                on_notify=lambda method, params: got.append((method, params)))
            pad = await dogproto.Client.connect(self.stack.sock("mediad"))
            try:
                await sub.call("media.subscribe")
                await pad.call("audio.ptt", {"down": True})
                await asyncio.sleep(hold_s)
                await pad.call("audio.ptt", {"down": False})
                await asyncio.sleep(wait_s)
            finally:
                await sub.close()
                await pad.close()

        asyncio.run(scenario())
        return got

    def test_ptt_sends_bargein_then_utterance(self):
        got = self.subscribe_and_press(0.5, 0.3)
        self.assertEqual([method for method, _ in got], ["media.bargein", "media.utterance"])
        utterance = got[1][1]
        self.assertEqual(utterance["rate"], 16000)
        seconds = len(base64.b64decode(utterance["pcm16_b64"])) / 2 / 16000
        self.assertAlmostEqual(seconds, 0.5, delta=0.15)
        self.assertTrue(utterance["id"])

    def test_short_press_dropped(self):
        got = self.subscribe_and_press(0.1, 0.3)
        self.assertEqual([method for method, _ in got], ["media.bargein"])

    def test_ptt_cuts_playback_within_100ms(self):
        """audio.play, then ptt down: the fake speaker logs the cut; 10 trials."""
        two_s = base64.b64encode(bytes(2 * 24000 * 2)).decode()
        samples = []

        async def trials():
            cloud = await dogproto.Client.connect(self.stack.sock("mediad", cloud=True))
            pad = await dogproto.Client.connect(self.stack.sock("mediad"))
            try:
                for _ in range(10):
                    plays = len([r for r in self.stack.audio_log() if r[1] == "play"])
                    await cloud.call("audio.play", {"pcm16_b64": two_s})
                    while len([r for r in self.stack.audio_log() if r[1] == "play"]) == plays:
                        await asyncio.sleep(0.005)
                    before = time.monotonic_ns()
                    await pad.call("audio.ptt", {"down": True})
                    end = time.monotonic() + 1.0
                    while time.monotonic() < end:
                        cuts = [t for t, event, _ in self.stack.audio_log() if event == "cut" and t >= before]
                        if cuts:
                            samples.append((cuts[0] - before) / 1e6)
                            break
                        await asyncio.sleep(0.005)
                    await pad.call("audio.ptt", {"down": False})
            finally:
                await cloud.close()
                await pad.close()

        asyncio.run(trials())
        print(f"\nptt cut latency ms: p50={statistics.median(samples):.2f} max={max(samples):.2f}")
        self.assertEqual(len(samples), 10)
        self.assertLessEqual(max(samples), 100.0)

    def test_play_rules(self):
        quarter_s = base64.b64encode(bytes(int(0.25 * 24000) * 2)).decode()
        one_s = base64.b64encode(bytes(24000 * 2)).decode()
        bad = ({"pcm16_b64": "***"}, {"pcm16_b64": base64.b64encode(b"abc").decode()},
               {"pcm16_b64": quarter_s, "rate": 1000},
               {"pcm16_b64": base64.b64encode(bytes(31 * 8000 * 2)).decode(), "rate": 8000})
        for params in bad:
            with self.assertRaises(dogproto.RpcError) as ctx:
                self.media("audio.play", params, cloud=True)
            self.assertEqual(ctx.exception.code, dogproto.INVALID_PARAMS)
        since = time.monotonic_ns()
        self.media("audio.play", {"pcm16_b64": quarter_s}, cloud=True)
        self.stack.wait_for(lambda: [e for t, e, _ in self.stack.audio_log() if t >= since] == ["play", "end"],
                            "play then end", timeout=2)
        self.media("audio.play", {"pcm16_b64": one_s}, cloud=True)
        self.stack.wait_for(lambda: [e for t, e, _ in self.stack.audio_log() if t >= since][-1:] == ["play"],
                            "second play", timeout=2)
        self.assertEqual(self.media("audio.stop", cloud=True), {})
        self.stack.wait_for(lambda: self.stack.audio_log()[-1][1] == "cut", "cut after audio.stop", timeout=1)

    def test_play_refused_while_ptt_down(self):
        quarter_s = base64.b64encode(bytes(int(0.25 * 24000) * 2)).decode()

        async def scenario():
            pad = await dogproto.Client.connect(self.stack.sock("mediad"))
            cloud = await dogproto.Client.connect(self.stack.sock("mediad", cloud=True))
            try:
                await pad.call("audio.ptt", {"down": True})
                with self.assertRaises(dogproto.RpcError) as ctx:
                    await cloud.call("audio.play", {"pcm16_b64": quarter_s})
                await pad.call("audio.ptt", {"down": False})
                await asyncio.sleep(0.1)
                await cloud.call("audio.play", {"pcm16_b64": quarter_s})
                return ctx.exception.code
            finally:
                await pad.close()
                await cloud.close()

        self.assertEqual(asyncio.run(scenario()), dogproto.BUSY)
        self.stack.wait_for(lambda: self.stack.audio_log()[-1][1] == "end", "play after the press", timeout=2)

    def test_cloud_socket_refuses_ptt(self):
        with self.assertRaises(dogproto.RpcError) as ctx:
            self.media("audio.ptt", {"down": True}, cloud=True)
        self.assertEqual(ctx.exception.code, dogproto.METHOD_NOT_FOUND)

    # ---- console

    def request(self, method, path, headers=None, body=None):
        """Send one HTTP request to the console; return (status, headers, body)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.stack.console_port, timeout=40)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.headers, resp.read()
        finally:
            conn.close()

    def cookie(self):
        """Log in with the token and return the Cookie header value."""
        status, headers, _ = self.request("GET", "/?token=" + self.stack.console_token)
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";")[0]

    def test_console_needs_token(self):
        for method, path in (("GET", "/"), ("GET", "/state"), ("GET", "/snapshot.jpg"), ("POST", "/stop"),
                             ("POST", "/arm")):
            self.assertEqual(self.request(method, path)[0], 401, f"{method} {path}")
        self.assertEqual(self.request("GET", "/?token=wrong")[0], 401)
        self.assertEqual(self.request("POST", "/arm", {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request("GET", "/state", {"Cookie": "dog_console=wrong"})[0], 401)

    def test_console_cookie_flow(self):
        status, headers, body = self.request("GET", "/?token=" + self.stack.console_token)
        self.assertEqual(status, 200)
        self.assertIn(b"STOP", body)
        set_cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        token = self.stack.console_token
        derived = hmac.new(token.encode(), b"cookie", hashlib.sha256).hexdigest()
        self.assertEqual(set_cookie.split(";")[0], "dog_console=" + derived)
        self.assertNotIn(token, set_cookie)
        self.assertEqual(self.request("GET", "/state", {"Cookie": "dog_console=" + token})[0], 401)
        cookie = {"Cookie": set_cookie.split(";")[0]}
        status, headers, body = self.request("GET", "/", cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"STOP", body)
        status, headers, body = self.request("GET", "/snapshot.jpg", cookie)
        self.assertEqual((status, headers["Content-Type"], body[:2]), (200, "image/jpeg", b"\xff\xd8"))
        status, _, body = self.request("GET", "/state", cookie)
        self.assertEqual(status, 200)
        self.assertIn(b'"armed"', body)
        self.assertIn(b'"frame_age_ms"', body)

    def test_console_stop_arm_and_trick(self):
        cookie = {"Cookie": self.cookie()}
        status, _, body = self.request("POST", "/stop", cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"stop_transmitted", body)
        status, _, body = self.request("POST", "/arm", cookie)
        self.assertEqual(status, 200)
        self.assertTrue(self.stack.call("robot.state")["armed"])
        status, _, body = self.request("POST", "/do", {**cookie, "Content-Type": "application/json"},
                                       b'{"trick": "bark"}')
        self.assertEqual(status, 200)
        self.assertIn(b'"transmitted": true', body)
        self.assertEqual(self.request("POST", "/do", cookie, b"[]")[0], 400)
        bearer = {"Authorization": "Bearer " + self.stack.console_token}
        self.assertEqual(self.request("POST", "/disarm", bearer)[0], 200)
        self.assertFalse(self.stack.call("robot.state")["armed"])

    def test_console_answers_a_deeply_nested_body(self):
        status = self.request("POST", "/do", {"Cookie": self.cookie()}, b"[" * 1000)[0]
        self.assertIn(status, (400, 500))

    def test_console_connection_limit(self):
        port = self.stack.console_port
        idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(mediad.CONSOLE_MAX_CONNECTIONS)]
        try:
            time.sleep(0.3)
            with socket.create_connection(("127.0.0.1", port)) as extra:
                extra.settimeout(2)
                self.assertEqual(extra.recv(64), b"", "connection over the limit is closed at once")
            idle[0].settimeout(mediad.HTTP_READ_S + 2)
            start = time.monotonic()
            self.assertTrue(idle[0].recv(64).startswith(b"HTTP/1.1 400"))
            self.assertLess(time.monotonic() - start, mediad.HTTP_READ_S + 1)
        finally:
            for conn in idle:
                conn.close()
        bearer = {"Authorization": "Bearer " + self.stack.console_token}
        self.stack.wait_for(lambda: self.request("GET", "/state", bearer)[0] == 200, "console after the limit")

    def test_short_token_turns_the_console_off(self):
        port = free_port()
        run_dir = tempfile.mkdtemp()
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "pi", "mediad.py"), "--fake-camera", "--fake-audio",
             "--config", os.path.join(run_dir, "absent.toml"), "--console-bind", "127.0.0.1",
             "--console-port", str(port)],
            env={**os.environ, "DOG_RUN_DIR": run_dir, "DOG_CONSOLE_TOKEN": "x" * 31},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            self.stack.wait_for(lambda: os.path.exists(os.path.join(run_dir, "mediad-cloud.sock")), "mediad start")
            time.sleep(0.2)
            with self.assertRaises(ConnectionRefusedError):
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
        finally:
            proc.terminate()
            _, err = proc.communicate(timeout=5)
        self.assertIn("must have 32 characters or more", err)

    def test_console_refuses_cross_origin_post(self):
        self.stack.call("robot.disarm")
        cookie = {"Cookie": self.cookie()}
        self.assertEqual(self.request("POST", "/arm", {**cookie, "Origin": "http://evil.example"})[0], 403)
        self.assertFalse(self.stack.call("robot.state")["armed"])
        same = f"http://127.0.0.1:{self.stack.console_port}"
        self.assertEqual(self.request("POST", "/stop", {**cookie, "Origin": same})[0], 200)


if __name__ == "__main__":
    unittest.main()
