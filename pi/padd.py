"""padd: the wired USB gamepad of the robot dog.

It maps a Logitech F310 class pad (XInput mode, read with python-evdev) to local
bus calls on robotd and mediad. On a lost pad or a stop it releases push-to-talk
and the stick. docs/design/media.md owns the mapping.
"""

import uuid

DEADBAND = 0.15
SELECT_HOLD_S = 1.0
EV_KEY, EV_ABS = 1, 3
ABS_X, ABS_Y = 0, 1
#: Linux codes that the xpad driver reports for an XInput pad.
BTN_A, BTN_B, BTN_X, BTN_Y = 304, 305, 307, 308
BTN_LB, BTN_RB, BTN_SELECT, BTN_START = 310, 311, 314, 315
#: Buttons that start a trick, by their key in [padd.bindings].
TRICK_BUTTONS = {"X": BTN_X, "Y": BTN_Y, "LB": BTN_LB, "RB": BTN_RB}


class PadMapper:
    """Pure mapping from evdev events and 20 Hz ticks to bus actions (service, method, params); no I/O."""

    def __init__(self, absinfo, bindings):
        """absinfo maps ABS_X and ABS_Y to (min, max); bindings maps X, Y, LB or RB to a trick name."""
        unknown = sorted(set(bindings) - set(TRICK_BUTTONS))
        if unknown:
            raise ValueError(f"unknown buttons in [padd.bindings]: {unknown}; use {sorted(TRICK_BUTTONS)}")
        if not all(isinstance(trick, str) and trick for trick in bindings.values()):
            raise ValueError("each [padd.bindings] value must be a trick name")
        self._absinfo = absinfo
        self._tricks = {TRICK_BUTTONS[name]: trick for name, trick in bindings.items()}
        self._axes = {ABS_X: 0.0, ABS_Y: 0.0}
        self._moving = False
        self._ptt_down = False
        self._select_since = None

    def on_event(self, etype, code, value, now):
        """Return the actions for one input event at monotonic time now."""
        if etype == EV_ABS and code in self._axes:
            low, high = self._absinfo[code]
            norm = max(-1.0, min(1.0, 2 * (value - low) / (high - low) - 1))
            self._axes[code] = 0.0 if abs(norm) < DEADBAND else norm
            return []
        if etype != EV_KEY or value not in (0, 1):  # 2 is key auto-repeat
            return []
        down = value == 1
        if code == BTN_A:
            self._ptt_down = down
            return [("mediad", "audio.ptt", {"down": down})]
        if code == BTN_SELECT:
            self._select_since = now if down else None
            return []
        if not down:
            return []
        if code == BTN_START:
            return [("robotd", "robot.enable", {})]
        if code == BTN_B:
            return [("robotd", "robot.stop", {})]
        if code in self._tricks:
            return [("robotd", "robot.do", {"trick": self._tricks[code], "id": uuid.uuid4().hex})]
        return []

    def on_tick(self, now):
        """Return the actions due at one tick: the stick intent and the Select hold."""
        actions = []
        vx = 0.0 - self._axes[ABS_Y]  # stick up is negative ABS_Y
        vyaw = 0.0 - self._axes[ABS_X]  # stick left is negative ABS_X and a positive (left) yaw
        if vx or vyaw:
            actions.append(("robotd", "robot.move", {"vx": round(vx, 3), "vyaw": round(vyaw, 3)}))
            self._moving = True
        elif self._moving:
            actions.append(("robotd", "robot.move", {"vx": 0.0, "vyaw": 0.0}))
            self._moving = False
        if self._select_since is not None and now - self._select_since >= SELECT_HOLD_S:
            actions.append(("robotd", "robot.disarm", {}))
            self._select_since = None  # once for each hold
        return actions

    def release(self):
        """Return the actions that end held controls, for a lost pad or a stop of padd; the mapper is then done."""
        actions = []
        if self._ptt_down:
            actions.append(("mediad", "audio.ptt", {"down": False}))
        if self._moving:
            actions.append(("robotd", "robot.move", {"vx": 0.0, "vyaw": 0.0}))
        return actions
