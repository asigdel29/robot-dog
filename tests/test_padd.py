"""Tests of the padd mapping from gamepad events to bus actions; they need no gamepad and no evdev."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pi"))

import padd  # noqa: E402
from padd import ABS_X, ABS_Y, EV_ABS, EV_KEY  # noqa: E402

XINPUT = {ABS_X: (-32768, 32767), ABS_Y: (-32768, 32767)}


def mapper(bindings=None, absinfo=XINPUT):
    """Return a mapper for an XInput pad."""
    return padd.PadMapper(absinfo, bindings or {})


def press(pad, code, now=0.0):
    """Return the actions of one button press."""
    return pad.on_event(EV_KEY, code, 1, now)


def release(pad, code, now=0.0):
    """Return the actions of one button release."""
    return pad.on_event(EV_KEY, code, 0, now)


class Stick(unittest.TestCase):
    """Left stick to robot.move: normalization, signs, dead band, 20 Hz and one zero on release."""

    def test_full_deflection_and_signs(self):
        pad = mapper()
        pad.on_event(EV_ABS, ABS_Y, -32768, 0.0)  # up
        self.assertEqual(pad.on_tick(0.0), [("robotd", "robot.move", {"vx": 1.0, "vyaw": 0.0})])
        pad.on_event(EV_ABS, ABS_Y, 0, 0.0)
        pad.on_event(EV_ABS, ABS_X, -32768, 0.0)  # left: positive yaw, a left turn
        self.assertEqual(pad.on_tick(0.05), [("robotd", "robot.move", {"vx": 0.0, "vyaw": 1.0})])
        pad.on_event(EV_ABS, ABS_X, 32767, 0.0)
        self.assertEqual(pad.on_tick(0.1)[0][2]["vyaw"], -1.0)

    def test_dead_band_and_release(self):
        pad = mapper()
        pad.on_event(EV_ABS, ABS_X, int(0.1 * 32767), 0.0)
        self.assertEqual(pad.on_tick(0.0), [])
        pad.on_event(EV_ABS, ABS_Y, int(0.5 * 32767), 0.0)  # down: back at half speed
        moves = [pad.on_tick(t / 20) for t in range(3)]
        self.assertTrue(all(m[0][2]["vx"] == -0.5 and m[0][2]["vyaw"] == 0.0 for m in moves))
        pad.on_event(EV_ABS, ABS_Y, 100, 0.2)
        self.assertEqual(pad.on_tick(0.25), [("robotd", "robot.move", {"vx": 0.0, "vyaw": 0.0})])
        self.assertEqual(pad.on_tick(0.3), [])

    def test_normalized_by_absinfo(self):
        pad = mapper(absinfo={ABS_X: (0, 255), ABS_Y: (0, 255)})
        pad.on_event(EV_ABS, ABS_X, 128, 0.0)
        pad.on_event(EV_ABS, ABS_Y, 0, 0.0)
        self.assertEqual(pad.on_tick(0.0), [("robotd", "robot.move", {"vx": 1.0, "vyaw": 0.0})])


class Buttons(unittest.TestCase):
    """Start, B, Select hold, A hold and trick bindings."""

    def test_start_arms_and_b_stops(self):
        pad = mapper()
        self.assertEqual(press(pad, padd.BTN_START), [("robotd", "robot.enable", {})])
        self.assertEqual(release(pad, padd.BTN_START), [])
        self.assertEqual(press(pad, padd.BTN_B), [("robotd", "robot.stop", {})])
        self.assertEqual(pad.on_event(EV_KEY, padd.BTN_B, 2, 0.0), [], "key repeat is ignored")

    def test_select_hold_disarms_once(self):
        pad = mapper()
        self.assertEqual(press(pad, padd.BTN_SELECT, 10.0), [])
        self.assertEqual(pad.on_tick(10.5), [])
        self.assertEqual(pad.on_tick(11.0), [("robotd", "robot.disarm", {})])
        self.assertEqual(pad.on_tick(11.5), [])
        release(pad, padd.BTN_SELECT, 12.0)
        press(pad, padd.BTN_SELECT, 20.0)
        release(pad, padd.BTN_SELECT, 20.6)
        self.assertEqual(pad.on_tick(21.5), [], "a short press does not disarm")

    def test_a_hold_is_push_to_talk(self):
        pad = mapper()
        self.assertEqual(press(pad, padd.BTN_A), [("mediad", "audio.ptt", {"down": True})])
        self.assertEqual(release(pad, padd.BTN_A), [("mediad", "audio.ptt", {"down": False})])

    def test_bindings(self):
        pad = mapper({"X": "bark", "RB": "dance"})
        (service, method, params), = press(pad, padd.BTN_X)
        self.assertEqual((service, method, params["trick"]), ("robotd", "robot.do", "bark"))
        self.assertIsInstance(params["id"], str)
        self.assertEqual(press(pad, padd.BTN_RB)[0][2]["trick"], "dance")
        self.assertNotEqual(press(pad, padd.BTN_X)[0][2]["id"], params["id"], "each press is a new action")
        self.assertEqual(press(pad, padd.BTN_Y), [], "unbound")
        self.assertEqual(press(mapper(), padd.BTN_LB), [], "empty bindings by default")

    def test_bad_bindings_refused(self):
        with self.assertRaises(ValueError):
            mapper({"Z": "bark"})
        with self.assertRaises(ValueError):
            mapper({"X": 3})


if __name__ == "__main__":
    unittest.main()
