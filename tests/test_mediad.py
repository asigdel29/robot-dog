"""Tests of mediad: marker math on drawn frames, snapshot, push-to-talk, barge-in, cloud rules and console auth.

The stack tests run mediad with --fake-camera and --fake-audio, so they need no
camera, microphone or speaker. The barge-in latency is printed so a run can go
into docs/test-log.md.
"""

import asyncio
import base64
import http.client
import math
import os
import statistics
import sys
import time
import unittest

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))
sys.path.insert(0, os.path.join(ROOT, "sim"))

import dogproto  # noqa: E402
import mediad  # noqa: E402
from devstack import Stack  # noqa: E402


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


class MediaStack(unittest.TestCase):
    """mediad in the dev stack: snapshot, audio, cloud rules and the console."""

    @classmethod
    def setUpClass(cls):
        cls.stack = Stack()
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

    def test_console_refuses_cross_origin_post(self):
        self.stack.call("robot.disarm")
        cookie = {"Cookie": self.cookie()}
        self.assertEqual(self.request("POST", "/arm", {**cookie, "Origin": "http://evil.example"})[0], 403)
        self.assertFalse(self.stack.call("robot.state")["armed"])
        same = f"http://127.0.0.1:{self.stack.console_port}"
        self.assertEqual(self.request("POST", "/stop", {**cookie, "Origin": same})[0], 200)


if __name__ == "__main__":
    unittest.main()
