"""hub: the server side of the dog, one process on 127.0.0.1:8000 behind Caddy.

It holds the WebSocket link from linkd on the Pi (/device), serves the robot
tools to Hermes Agent over the Model Context Protocol (/tools, loopback, bearer
TOOLS_TOKEN) and runs the voice loop: utterance, speech to text, Hermes run,
text to speech, audio.play. Every client is async: Hermes calls back into /tools
while the voice loop waits on the same Hermes run, in this same process.
docs/design/cloud.md owns the protocol, the tool contracts and the limits.
"""

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

log = logging.getLogger("hub")

API_VERSION = 1
DEVICE_ID = "dog-01"
DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN", "")

HELLO_WAIT_S = 10
FRAME_LIMIT = 2 * 1024 * 1024
PING_S = 5


class Device:
    """The link to the one Pi: it sends cloud requests and matches the replies by id."""

    def __init__(self):
        self.ws = None

    def detach(self, ws):
        """Forget a closed link and fail its open requests."""
        if self.ws is ws:
            self.ws = None


device = Device()


def _refuse_constant(name):
    """Refuse NaN and Infinity in a frame from the Pi, as the Pi bus does (dogproto.decode)."""
    raise ValueError(f"{name} is not valid JSON")


async def _receive(ws):
    """Return the next frame from the Pi as a dict, or None for a binary frame or one that is not a JSON object."""
    try:
        msg = json.loads(await ws.receive_text(), parse_constant=_refuse_constant)
    except (KeyError, ValueError):  # KeyError: a binary frame has no text
        return None
    return msg if isinstance(msg, dict) else None


def _bearer_ok(header, token):
    """Return True when an Authorization header carries this token; an unset token never matches."""
    return bool(token) and hmac.compare_digest(header.encode(), f"Bearer {token}".encode())


async def healthz(request):
    """Liveness for Caddy and the deploy check."""
    return JSONResponse({"ok": True})


async def device_link(ws):
    """Serve the Pi link: bearer DEVICE_TOKEN, then link.hello; a new link replaces the old one."""
    if not _bearer_ok(ws.headers.get("authorization", ""), DEVICE_TOKEN):
        await ws.close(code=1008)  # before accept, so the Pi gets HTTP 403
        return
    await ws.accept()
    try:
        hello = await asyncio.wait_for(_receive(ws), HELLO_WAIT_S) or {}
    except (TimeoutError, WebSocketDisconnect):
        hello = {}
    params = hello.get("params")
    if hello.get("method") != "link.hello" or not isinstance(params, dict) or params.get("device") != DEVICE_ID:
        with contextlib.suppress(RuntimeError):
            await ws.close(code=1008)
        return
    if params.get("api") != API_VERSION:
        log.warning("device api %s differs from hub api %d", params.get("api"), API_VERSION)
    old, device.ws = device.ws, ws
    if old is not None:
        device.detach(old)
        with contextlib.suppress(RuntimeError, OSError):
            await old.close(code=4000, reason="replaced")
    log.info("device %s up, version %s", DEVICE_ID, params.get("version"))
    try:
        while True:
            msg = await _receive(ws)
            if msg is None:
                continue
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        device.detach(ws)
        log.warning("device %s link down", DEVICE_ID)


app = Starlette(routes=[
    Route("/healthz", healthz),
    WebSocketRoute("/device", device_link),
])


def main():
    """Check the environment (/etc/doghub/hub.env) and serve on loopback."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    missing = [name for name in ("DEVICE_TOKEN", "TOOLS_TOKEN", "HERMES_API_KEY", "OPENAI_API_KEY")
               if not os.environ.get(name)]
    if missing:
        sys.exit(f"hub: set {', '.join(missing)}")
    # force: the tool server set up logging on import, which would make this call do nothing.
    logging.basicConfig(level=os.environ.get("DOG_LOG", "INFO"), force=True,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=args.port, ws_max_size=FRAME_LIMIT,
                ws_ping_interval=PING_S, ws_ping_timeout=PING_S)


if __name__ == "__main__":
    main()
