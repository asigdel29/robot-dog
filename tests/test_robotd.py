"""Unit tests for robotd, dogproto and robotctl: registry rules, geometry, the validator and in-process ordering."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))

import dogproto  # noqa: E402


class Params(unittest.TestCase):
    """The dogproto PARAMS validator."""

    def test_validate(self):
        """Unknown, absent and mistyped fields and unknown methods are refused; a final "?" marks an optional field."""
        for method, params, code in (
                ("robot.do", {"trick": "bark", "id": "1", "x": 1}, dogproto.INVALID_PARAMS),
                ("robot.do", {"trick": "bark"}, dogproto.INVALID_PARAMS),
                ("robot.drive", {"dir": "forward", "ms": True, "id": "1"}, dogproto.INVALID_PARAMS),
                ("robot.fly", {}, dogproto.METHOD_NOT_FOUND)):
            with self.subTest(method=method, params=params):
                with self.assertRaises(dogproto.RpcError) as ctx:
                    dogproto.validate(method, params)
                self.assertEqual(ctx.exception.code, code)
        self.assertEqual(dogproto.validate("media.snapshot", {}), {})
        self.assertEqual(dogproto.validate("media.snapshot", {"max_px": 320}), {"max_px": 320})

    def test_nan_never_reaches_the_stick(self):
        """A clamp turns NaN into full motion, so the bus refuses NaN and Infinity."""
        with self.assertRaises(ValueError):
            dogproto.decode('{"vx":NaN,"vyaw":0}')
        for params in (dogproto.decode('{"vx":1e999,"vyaw":0}'), {"vx": float("nan"), "vyaw": 0.0}):
            with self.assertRaises(dogproto.RpcError):
                dogproto.validate("robot.move", params)

    def test_cloud_methods_exclude_arming_and_stick(self):
        for method in ("robot.enable", "robot.move", "robot.capture", "robot.test", "robot.reload", "audio.ptt"):
            self.assertNotIn(method, dogproto.CLOUD_METHODS)


if __name__ == "__main__":
    unittest.main()
