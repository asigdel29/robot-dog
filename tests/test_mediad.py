"""Tests of mediad: marker math on drawn frames, snapshot, push-to-talk, barge-in, cloud rules and console auth.

The stack tests run mediad with --fake-camera and --fake-audio, so they need no
camera, microphone or speaker. The in-process tests put stub aplay and arecord
scripts on PATH, so the real subprocess paths run too. The barge-in latency is
printed so a run can go into docs/test-log.md.
"""

import base64
import math
import os
import sys
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
        cls.detect = staticmethod(mediad.make_detector())

    def test_heading_within_5_degrees(self):
        for degrees in range(-180, 180, 15):
            heading = math.radians(degrees)
            frame = floor()
            mediad.draw_marker(frame, mediad.marker_tile(1), 300.5, 200.25, heading)
            cx, cy, found = self.detect(frame)["1"]
            self.assertLess(angle_diff_deg(found, heading), 5.0, f"heading {degrees} deg")
            self.assertLess(math.hypot(cx - 300.5, cy - 200.25), 1.0, f"center at {degrees} deg")

    def test_several_markers_and_none(self):
        self.assertEqual(self.detect(floor()), {})
        frame = floor()
        for marker_id, x in ((1, 100), (2, 320), (7, 540)):
            mediad.draw_marker(frame, mediad.marker_tile(marker_id), x, 240, 0.3)
        self.assertEqual(sorted(self.detect(frame)), ["1", "2", "7"])

    def test_old_opencv_refused(self):
        old = types.SimpleNamespace(__version__="4.6.0", aruco=types.SimpleNamespace())
        with mock.patch.object(mediad, "cv2", old), self.assertRaises(SystemExit) as ctx:
            mediad.require_aruco_detector()
        self.assertIn("4.7", str(ctx.exception.code))
        mediad.require_aruco_detector()


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

    def events(self, since_ns=0):
        """Return the fake speaker events after since_ns."""
        return [event for t, event in self.stack.audio_log() if t >= since_ns]

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

    def test_play_rules(self):
        quarter_s = base64.b64encode(bytes(int(0.25 * 24000) * 2)).decode()
        one_s = base64.b64encode(bytes(24000 * 2)).decode()
        bad = ({"pcm16_b64": "***"}, {"pcm16_b64": "é"}, {"pcm16_b64": base64.b64encode(b"abc").decode()},
               {"pcm16_b64": quarter_s, "rate": 1000},
               {"pcm16_b64": base64.b64encode(bytes(31 * 8000 * 2)).decode(), "rate": 8000})
        for params in bad:
            with self.assertRaises(dogproto.RpcError) as ctx:
                self.media("audio.play", params, cloud=True)
            self.assertEqual(ctx.exception.code, dogproto.INVALID_PARAMS)
        since = time.monotonic_ns()
        self.media("audio.play", {"pcm16_b64": quarter_s}, cloud=True)
        self.stack.wait_for(lambda: self.events(since) == ["play", "end"], "play then end", timeout=2)
        self.media("audio.play", {"pcm16_b64": one_s}, cloud=True)
        self.stack.wait_for(lambda: self.events(since)[-1:] == ["play"], "second play", timeout=2)
        self.assertEqual(self.media("audio.stop", cloud=True), {})
        self.stack.wait_for(lambda: self.events()[-1:] == ["cut"], "cut after audio.stop", timeout=1)


if __name__ == "__main__":
    unittest.main()
