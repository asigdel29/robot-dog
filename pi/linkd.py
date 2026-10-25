"""linkd: the only process on the Pi with internet access; it links the dog to the hub.

It holds one WebSocket to the hub (DOG_HUB_URL, bearer DOG_DEVICE_TOKEN) and
forwards the hub's cloud requests to the cloud sockets of robotd and mediad. It
forwards mediad's utterances and barge-ins to the hub. When the hub link goes
down it disarms the dog and closes its local connections, so robotd cancels the
cloud actions. linkd cannot arm the dog: robot.enable is not a cloud method.
docs/design/cloud.md owns the protocol.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
import urllib.parse

import websockets
from websockets.asyncio.client import connect

import dogproto
from dogproto import RpcError

log = logging.getLogger("linkd")

PING_S = 5
#: A silent hub is dropped after ping interval + pong timeout + this close timeout.
CLOSE_S = 1
RECONNECT_MIN_S = 1
#: The release directory name (/opt/dog/releases/<ver>), or the checkout name in dev.
VERSION = os.path.basename(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


class Link:
    """One hub link at a time, its local daemon connections and the reconnect loop."""

    def __init__(self, url, token, device, clock_synced):
        self.url = url
        self.token = token
        self.device = device
        self.clock_synced = clock_synced
        self.ws = None
        self._tasks = set()

    async def run(self):
        """Connect to the hub until SIGTERM; wait 1 s to 30 s between attempts."""
        # SIGTERM cancels this task, so the session cleanup disarms the dog as for any link loss.
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
        delay = RECONNECT_MIN_S
        while True:
            try:
                async with connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
                                   max_size=dogproto.MEDIA_LIMIT, ping_interval=PING_S, ping_timeout=PING_S,
                                   close_timeout=CLOSE_S, open_timeout=10) as ws:
                    try:
                        await self._session(ws)
                    finally:
                        await self._drop_local()
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("hub link: %s", exc)
            await asyncio.sleep(delay)

    async def _session(self, ws):
        """Serve one hub connection until it closes."""
        log.info("hub link up: %s", self.url)
        self.ws = ws
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "link.hello", "params": {
            "api": dogproto.API_VERSION, "version": VERSION, "device": self.device}}))
        try:
            async for frame in ws:
                self._spawn(self._handle(ws, frame, time.time() * 1000))
        finally:
            self.ws = None
            for task in self._tasks:
                task.cancel()
            log.warning("hub link down")

    async def _drop_local(self):
        """Disarm the dog and close the local connections after the hub link ends."""
        try:
            await dogproto.call_once(dogproto.sock_path("robotd", cloud=True), "robot.disarm", timeout=2)
            log.info("disarmed after hub link loss")
        except (OSError, RpcError) as exc:
            log.error("disarm after hub link loss failed: %s", exc)

    def _spawn(self, coro):
        """Run coro as a task of this session; the session end cancels it."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, ws, frame, received_ms):
        """Check one hub request, send it without sent_at to the daemon that owns it, and send back the reply.

        A frame that is not a request is ignored.
        """
        try:
            msg = dogproto.decode(frame)
        except ValueError:
            return
        if not isinstance(msg, dict) or "id" not in msg:
            return


def main():
    """Check the environment (/etc/dog/link.env on the Pi) and run the link until SIGTERM."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--assume-synced", action="store_true", help="skip the NTP check (dev and tests only)")
    args = parser.parse_args()
    url = os.environ.get("DOG_HUB_URL", "")
    token = os.environ.get("DOG_DEVICE_TOKEN", "")
    parts = urllib.parse.urlsplit(url)
    # The token must not cross the net in clear: wss://, or ws:// to this machine for tests and dev.
    loopback = parts.scheme == "ws" and parts.hostname in ("127.0.0.1", "localhost", "::1")
    if not token or not (parts.scheme == "wss" or loopback):
        sys.exit("linkd: set DOG_HUB_URL (wss://...) and DOG_DEVICE_TOKEN")
    logging.basicConfig(level=os.environ.get("DOG_LOG", "INFO"), format="%(name)s %(levelname)s %(message)s")
    # The websockets DEBUG log prints the handshake headers, which include the device token.
    logging.getLogger("websockets").setLevel(logging.INFO)
    link = Link(url, token, os.environ.get("DOG_DEVICE", "dog-01"), args.assume_synced)
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(link.run())


if __name__ == "__main__":
    main()
