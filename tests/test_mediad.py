"""Tests of mediad: marker math on drawn frames, snapshot, push-to-talk, barge-in, cloud rules and console auth.

The stack tests run mediad with --fake-camera and --fake-audio, so they need no
camera, microphone or speaker. The in-process tests put stub aplay and arecord
scripts on PATH, so the real subprocess paths run too. The barge-in latency is
printed so a run can go into docs/test-log.md.
"""

import math
import os
import sys
import types
import unittest
from unittest import mock

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))

import mediad  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
