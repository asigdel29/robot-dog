"""padd: the wired USB gamepad of the robot dog.

It reads a Logitech F310 class pad (XInput mode) with python-evdev and turns
its events into local bus calls: robot.move notifications to robotd at 20 Hz
from the left stick, Start to arm, B to stop, Select held for 1 s to disarm, A
held for push-to-talk on mediad, and X, Y, LB and RB for the tricks in
[padd.bindings] of dog.toml. When the pad is lost or padd stops, padd
releases push-to-talk and the stick; if padd dies, the robotd deadman
(500 ms) stops the motion and mediad releases push-to-talk when the
connection closes. A request is sent once and never sent again. evdev is
imported only on the Pi, so the mapping tests run anywhere.
docs/design/media.md owns the mapping.
"""

import argparse
import asyncio
import contextlib
import functools
import logging
import os
import signal
import sys
import time
import tomllib
import uuid

import dogproto

log = logging.getLogger("padd")

TICK_HZ = 20
DEADBAND = 0.15
SELECT_HOLD_S = 1.0
RETRY_S = 1.0
REPLY_WAIT_S = 60.0
RELEASE_WAIT_S = 1.0
EV_KEY, EV_ABS = 1, 3
ABS_X, ABS_Y = 0, 1
#: Linux codes that the xpad driver reports for an XInput pad.
BTN_A, BTN_B, BTN_X, BTN_Y = 304, 305, 307, 308
BTN_LB, BTN_RB, BTN_SELECT, BTN_START = 310, 311, 314, 315
#: Buttons that start a trick, by their key in [padd.bindings].
TRICK_BUTTONS = {"X": BTN_X, "Y": BTN_Y, "LB": BTN_LB, "RB": BTN_RB}


class PadMapper:
    """Pure mapping from evdev-style events and 20 Hz ticks to bus actions.

    An action is (service, method, params). It touches no device and no socket,
    so tests drive it with plain numbers.
    """

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
        """Return the actions that end held controls, for a lost pad or a stop of padd."""
        actions = []
        if self._ptt_down:
            actions.append(("mediad", "audio.ptt", {"down": False}))
        if self._moving:
            actions.append(("robotd", "robot.move", {"vx": 0.0, "vyaw": 0.0}))
        self._axes = {ABS_X: 0.0, ABS_Y: 0.0}
        self._moving = self._ptt_down = False
        self._select_since = None
        return actions


def find_pad():
    """Return (device, absinfo) of the first device with two stick axes and gamepad buttons, or None."""
    import evdev  # Linux only

    for path in evdev.list_devices():
        device = evdev.InputDevice(path)
        caps = device.capabilities()
        axes = {code: (info.min, info.max) for code, info in caps.get(EV_ABS, []) if code in (ABS_X, ABS_Y)}
        if len(axes) == 2 and {BTN_A, BTN_B, BTN_START} <= set(caps.get(EV_KEY, [])):
            return device, axes
        device.close()
    return None


async def report(method, call):
    """Wait for one reply and log it; a failed request is not sent again."""
    try:
        log.info("%s: %s", method, await call)
    except (dogproto.RpcError, ConnectionError, TimeoutError) as exc:
        log.warning("%s failed: %s", method, exc)


def dispatch(clients, actions):
    """Send actions without waiting: robot.move as a notification, the rest as requests; return the request tasks."""
    tasks = []
    for service, method, params in actions:
        client = clients.get(service)
        if client is None or client.closed.is_set():
            log.warning("%s not connected; %s dropped", service, method)
            continue
        if method == "robot.move":
            client.notify(method, params)
        else:
            tasks.append(asyncio.ensure_future(report(method, client.call(method, params, REPLY_WAIT_S))))
    return tasks


async def tick_loop(mapper, clients):
    """Run the mapper ticks at 20 Hz."""
    while True:
        dispatch(clients, mapper.on_tick(time.monotonic()))
        await asyncio.sleep(1 / TICK_HZ)


async def run(bindings):
    """Find the pad, map its events, and look for it again each 1 s after an unplug."""
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    clients = {}
    keepers = [asyncio.ensure_future(dogproto.keep_connected(
        dogproto.sock_path(service), functools.partial(clients.__setitem__, service), RETRY_S))
        for service in ("robotd", "mediad")]
    try:
        while True:
            found = find_pad()
            if found is None:
                await asyncio.sleep(RETRY_S)
                continue
            device, absinfo = found
            log.info("gamepad %s at %s", device.name, device.path)
            mapper = PadMapper(absinfo, bindings)
            ticker = asyncio.ensure_future(tick_loop(mapper, clients))
            try:
                async for event in device.async_read_loop():
                    dispatch(clients, mapper.on_event(event.type, event.code, event.value, time.monotonic()))
            except OSError as exc:
                log.warning("gamepad lost: %s", exc)
            finally:
                ticker.cancel()
                device.close()
                pending = dispatch(clients, mapper.release())
                if pending:
                    await asyncio.wait(pending, timeout=RELEASE_WAIT_S)
            await asyncio.sleep(RETRY_S)
    finally:
        for keeper in keepers:
            keeper.cancel()


def load_bindings(path):
    """Return the [padd.bindings] table of dog.toml, or {} when the file is absent."""
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle).get("padd", {}).get("bindings", {})
    except FileNotFoundError:
        return {}


def main(argv=None):
    """Start padd; refuse a bad [padd.bindings] before the pad is open."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.environ.get("DOG_CONFIG", "/etc/dog/dog.toml"))
    args = parser.parse_args(argv)
    bindings = load_bindings(args.config)
    PadMapper({ABS_X: (-1, 1), ABS_Y: (-1, 1)}, bindings)
    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
        asyncio.run(run(bindings))


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("DOG_LOG", "INFO"), format="%(name)s %(levelname)s %(message)s")
    main(sys.argv[1:])
