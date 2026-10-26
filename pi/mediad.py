"""mediad: camera, ArUco pose, microphone, speaker and the LAN console of the robot dog.

It is the only writer of the pose: it sends the markers of each camera frame to
robotd as robot.observe. It records push-to-talk audio with arecord, plays queued
replies with aplay and serves the owner console. docs/design/media.md owns the rules.
"""

import argparse
import asyncio
import contextlib
import functools
import json
import logging
import math
import os
import sys
import threading
import time
import tomllib

import cv2
import numpy as np

import dogproto

log = logging.getLogger("mediad")

CAMERA_FPS = 15
FRAME_W, FRAME_H = 640, 480
CAMERA_RETRY_S = 1.0
ROBOTD_RETRY_S = 0.5
FAKE_ROBOT_MARKER = 1  # goto.robot_marker of the dev stack registry
FAKE_MARKER_PX = 36  # 6 px for each cell of a 4x4 marker with its border
FAKE_MARGIN_PX = 6  # white quiet zone around each fake marker
FAKE_FLOOR_GRAY = 150


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
    cv2.setNumThreads(1)  # detection on all cores would starve robotd and the bridge link
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


# ---------------------------------------------------------------- daemon


class Mediad:
    """All mediad state: the newest frame, the robotd link, subscribers, the play queue and push-to-talk."""

    def __init__(self, fake_audio, speaker, mic):
        self.fake_audio = fake_audio
        self.speaker = speaker
        self.mic = mic
        self.frame = None  # (t_mono_ns, BGR image)
        self.robotd = None

    def on_frame(self, t_ns, frame, markers):
        """Keep the newest frame and send its marker poses to robotd; never waits on robotd."""
        self.frame = (t_ns, frame)
        if self.robotd is not None:
            self.robotd.notify("robot.observe", {"t_mono_ns": t_ns, "markers": markers})


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
    open_source = FakeCamera if args.fake_camera else functools.partial(open_camera, cfg.get("camera", "/dev/video0"))
    deliver = functools.partial(asyncio.get_running_loop().call_soon_threadsafe, media.on_frame)
    threading.Thread(target=capture_loop, args=(open_source, deliver), daemon=True).start()
    log.info("mediad ready, api %d", dogproto.API_VERSION)
    await asyncio.gather(dogproto.keep_connected(
        dogproto.sock_path("robotd"), functools.partial(setattr, media, "robotd"), ROBOTD_RETRY_S))


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("DOG_LOG", "INFO"), format="%(name)s %(levelname)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(sys.argv[1:]))
