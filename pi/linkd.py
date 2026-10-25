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
RECONNECT_MAX_S = 30
#: Only a session longer than this resets the reconnect wait, so a hub that closes at once gets the backoff.
STABLE_SESSION_S = 2 * PING_S
#: Longer than the hub wait (35 s), which is longer than the robotd goto limit (30 s).
LOCAL_TIMEOUT_S = 40
#: mediad notifications and the hub method each one becomes.
FORWARDS = {"media.utterance": "voice.utterance", "media.bargein": "voice.cancel"}
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
        self._local = {}
        self._local_lock = asyncio.Lock()
        self._tasks = set()

    async def run(self):
        """Connect to the hub until SIGTERM; wait 1 s to 30 s between attempts."""
        # SIGTERM cancels this task, so the session cleanup disarms the dog as for any link loss.
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
        delay = RECONNECT_MIN_S
        while True:
            started = None
            try:
                async with connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
                                   max_size=dogproto.MEDIA_LIMIT, ping_interval=PING_S, ping_timeout=PING_S,
                                   close_timeout=CLOSE_S, open_timeout=10) as ws:
                    started = time.monotonic()
                    try:
                        await self._session(ws)
                    finally:
                        await self._drop_local()
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("hub link: %s", exc)
            if started is not None and time.monotonic() - started > STABLE_SESSION_S:
                delay = RECONNECT_MIN_S
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def _session(self, ws):
        """Serve one hub connection until it closes."""
        log.info("hub link up: %s", self.url)
        self.ws = ws
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "link.hello", "params": {
            "api": dogproto.API_VERSION, "version": VERSION, "device": self.device}}))
        self._spawn(self._subscribe())
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
        async with self._local_lock:
            for client in self._local.values():
                await client.close()
            self._local.clear()

    def _spawn(self, coro):
        """Run coro as a task of this session; the session end cancels it."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _client(self, service):
        """Return the open connection to a daemon's cloud socket; open it on first use."""
        async with self._local_lock:
            client = self._local.get(service)
            if client is None or client.closed.is_set():
                on_notify = self._on_notify if service == "mediad" else None
                client = await dogproto.Client.connect(dogproto.sock_path(service, cloud=True), on_notify=on_notify)
                self._local[service] = client
            return client

    async def _subscribe(self):
        """Keep a media.subscribe open on mediad, so utterances and barge-ins reach the hub."""
        while True:
            try:
                client = await self._client("mediad")
                await client.call("media.subscribe")
                await client.closed.wait()
            except (OSError, RpcError) as exc:
                log.debug("mediad subscribe: %s", exc)
            await asyncio.sleep(1)

    def _on_notify(self, method, params):
        """Forward one mediad notification to the hub."""
        name = FORWARDS.get(method)
        ws = self.ws
        if name is not None and ws is not None:
            self._spawn(self._send(ws, {"jsonrpc": "2.0", "method": name, "params": params}))

    async def _send(self, ws, msg):
        """Send one message to the hub; a link that closed meanwhile drops it."""
        with contextlib.suppress(websockets.exceptions.ConnectionClosed):
            await ws.send(json.dumps(msg, separators=(",", ":")))

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
        method = msg.get("method")
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        try:
            # linkd owns the mediad subscription; a second one from the hub would duplicate each utterance.
            if not isinstance(method, str) or method not in dogproto.CLOUD_METHODS or method == "media.subscribe":
                raise RpcError(dogproto.METHOD_NOT_FOUND, f"unknown method {method}")
            client = await self._client("robotd" if method.startswith("robot.") else "mediad")
            reply["result"] = await client.call(method, msg.get("params"), timeout=LOCAL_TIMEOUT_S)
        except RpcError as err:
            reply["error"] = err.to_obj()
        except ValueError as exc:  # for example 1e999 in params: the local bus refuses Infinity
            reply["error"] = RpcError(dogproto.INVALID_PARAMS, str(exc)).to_obj()
        except TimeoutError:
            reply["error"] = RpcError(dogproto.TIMEOUT, f"no daemon reply in {LOCAL_TIMEOUT_S} s").to_obj()
        except OSError as exc:
            reply["error"] = RpcError(dogproto.UNAVAILABLE, f"daemon not reachable: {exc}").to_obj()
        await self._send(ws, reply)


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
