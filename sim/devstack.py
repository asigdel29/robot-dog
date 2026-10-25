"""Run the dog stack on one machine against the fake ESP32, for tests and local work.

``python sim/devstack.py`` starts fake_esp32 and robotd with DEMO_REGISTRY in a
temporary run directory and prints how to reach them. Tests import Stack. Each
process gets DOG_RUN_DIR, so nothing touches /run/dog or /etc/dog.
"""


def _nec(cmd, **extra):
    return {"ir": {"proto": "NEC", "addr": 0, "cmd": cmd, "bits": 32, "raw_us": [], "carrier_khz": 38},
            "repeat": "nec", "period_ms": 108, "verified": True, **extra}


#: Verified commands for the fake toy. Real IR codes come from robotctl registry capture.
DEMO_REGISTRY = {
    "schema": 1,
    "robot": {"model": "supireo-y8359", "id": "dog-01"},
    "commands": {
        "forward": _nec(0x18, kind="hold", max_run_ms=1000, cloud=True, say="walks forward"),
        "back": _nec(0x52, kind="hold", max_run_ms=1000, cloud=True, say="walks back"),
        "left": _nec(0x08, kind="hold", max_run_ms=1000, cloud=True, say="turns left"),
        "right": _nec(0x5A, kind="hold", max_run_ms=1000, cloud=True, say="turns right"),
        "bark": _nec(0x45, kind="episodic", repeat="none", natural_ms=1500, cloud=True, say="barks"),
        "dance": _nec(0x46, kind="perpetual", repeat="none", stop_via="stop", natural_ms=3000,
                      max_run_ms=3000, cloud=True, say="dances"),
        "stop": _nec(0x40, kind="episodic", repeat="none", natural_ms=0, say="stops"),
        "program": _nec(0x44, kind="mode", repeat="none", say="program mode"),
    },
    "drive": {"forward": "forward", "back": "back", "left": "left", "right": "right"},
    "goto": {"robot_marker": 1, "goal_radius_px": 40, "area_px": [[0, 0], [640, 0], [640, 480], [0, 480]],
             "turn_ms": 400, "forward_ms": 500, "settle_ms": 200, "turn_sign": 1},
}
