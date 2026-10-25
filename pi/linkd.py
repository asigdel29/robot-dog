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
import subprocess
import sys
import time
import urllib.parse

import websockets
from websockets.asyncio.client import connect

import dogproto
from dogproto import RpcError

log = logging.getLogger("linkd")

#: A hub request older than this (by its sent_at) is refused with STALE.
STALE_MS = 1500
PING_S = 5
RECONNECT_MIN_S = 1
RECONNECT_MAX_S = 30
#: Longer than the hub wait (35 s), which is longer than the robotd goto limit (30 s).
LOCAL_TIMEOUT_S = 40
CLOCK_CHECK_S = 30
#: Method prefix to the daemon that serves it.
ROUTES = {"robot": "robotd", "media": "mediad", "audio": "mediad"}
#: mediad notifications and the hub method each one becomes.
FORWARDS = {"media.utterance": "voice.utterance", "media.bargein": "voice.cancel"}


def release_version():
    """Return the release directory name (/opt/dog/releases/<ver>), or the checkout name in dev."""
    return os.path.basename(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def ntp_synced():
    """Return True when systemd says the clock is NTP-synced; STALE checks need a true clock."""
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.stdout.strip() == "yes"


class Link:
    """One hub link at a time, its local daemon connections and the reconnect loop."""

    def __init__(self, url, token, device, assume_synced):
        self.url = url
        self.token = token
        self.device = device
        self.assume_synced = assume_synced
        self.ws = None
        self._local = {}
        self._local_lock = asyncio.Lock()
        self._tasks = set()
        self._clock = (False, -CLOCK_CHECK_S)

    async def run(self):
        """Connect to the hub forever; wait 1 s to 30 s between attempts."""
        delay = RECONNECT_MIN_S
        while True:
            try:
                async with connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
                                   max_size=dogproto.MEDIA_LIMIT, ping_interval=PING_S, ping_timeout=PING_S,
                                   open_timeout=10) as ws:
                    delay = RECONNECT_MIN_S
                    try:
                        await self._session(ws)
                    finally:
                        await self._drop_local()
            except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as exc:
                log.warning("hub link: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    async def _session(self, ws):
        """Serve one hub connection until it closes."""
        log.info("hub link up: %s", self.url)
        self.ws = ws
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "link.hello", "params": {
            "api": dogproto.API_VERSION, "version": release_version(), "device": self.device}}))
        subscriber = asyncio.ensure_future(self._subscribe())
        try:
            async for frame in ws:
                self._spawn(self._handle(ws, frame))
        finally:
            self.ws = None
            subscriber.cancel()
            for task in list(self._tasks):
                task.cancel()
            log.warning("hub link down")

    async def _drop_local(self):
        """Disarm the dog and close the local connections after the hub link ends."""
        try:
            await dogproto.call_once(dogproto.sock_path("robotd", cloud=True), "robot.disarm", timeout=2)
            log.info("disarmed after hub link loss")
        except (OSError, RpcError, TimeoutError, ConnectionError) as exc:
            log.error("disarm after hub link loss failed: %s", exc)
        async with self._local_lock:
            for client in self._local.values():
                await client.close()
            self._local.clear()

    def _spawn(self, coro):
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
            except (OSError, RpcError, TimeoutError, ConnectionError) as exc:
                log.debug("mediad subscribe: %s", exc)
            await asyncio.sleep(1)

    def _on_notify(self, method, params):
        """Forward one mediad notification to the hub."""
        name = FORWARDS.get(method)
        ws = self.ws
        if name is not None and ws is not None:
            self._spawn(self._send(ws, {"jsonrpc": "2.0", "method": name, "params": params}))

    async def _send(self, ws, msg):
        with contextlib.suppress(websockets.exceptions.ConnectionClosed):
            await ws.send(json.dumps(msg, separators=(",", ":")))

    async def _handle(self, ws, frame):
        """Answer one hub request; frames that are not requests are ignored."""
        try:
            msg = dogproto.decode(frame)
        except ValueError:
            return
        if not isinstance(msg, dict) or "id" not in msg:
            return
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        try:
            reply["result"] = await self._forward(msg)
        except RpcError as err:
            reply["error"] = err.to_obj()
        except TimeoutError:
            reply["error"] = RpcError(dogproto.TIMEOUT, f"no daemon reply in {LOCAL_TIMEOUT_S} s").to_obj()
        except (OSError, ConnectionError) as exc:
            reply["error"] = RpcError(dogproto.UNAVAILABLE, f"daemon not reachable: {exc}").to_obj()
        await self._send(ws, reply)

    async def _forward(self, msg):
        """Check one hub request and send it, without sent_at, to the daemon that owns it."""
        method = msg.get("method")
        # linkd owns the mediad subscription; a second one from the hub would duplicate each utterance.
        if not isinstance(method, str) or method not in dogproto.CLOUD_METHODS or method == "media.subscribe":
            raise RpcError(dogproto.METHOD_NOT_FOUND, f"unknown method {method}")
        sent_at = msg.get("sent_at")
        if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)):
            raise RpcError(dogproto.STALE, "request has no sent_at")
        if not await self._clock_synced():
            raise RpcError(dogproto.STALE, "the Pi clock is not NTP-synced")
        age_ms = time.time() * 1000 - sent_at
        if age_ms > STALE_MS:
            raise RpcError(dogproto.STALE, f"request is {age_ms:.0f} ms old")
        client = await self._client(ROUTES[method.split(".", 1)[0]])
        return await client.call(method, msg.get("params"), timeout=LOCAL_TIMEOUT_S)

    async def _clock_synced(self):
        """Return the NTP sync state, checked again each CLOCK_CHECK_S in a thread so a stop never waits on it."""
        if self.assume_synced:
            return True
        synced, checked = self._clock
        if time.monotonic() - checked >= CLOCK_CHECK_S:
            synced = await asyncio.to_thread(ntp_synced)
            self._clock = (synced, time.monotonic())
        return synced


def hub_url_ok(url):
    """Return True for wss://, or ws:// to this machine (tests and dev); the token must not cross the net in clear."""
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "wss" or (parts.scheme == "ws" and parts.hostname in ("127.0.0.1", "localhost", "::1"))


async def main(argv=None):
    """Start linkd from the environment (/etc/dog/link.env on the Pi)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--assume-synced", action="store_true", help="skip the NTP check (dev and tests only)")
    args = parser.parse_args(argv)
    url = os.environ.get("DOG_HUB_URL", "")
    token = os.environ.get("DOG_DEVICE_TOKEN", "")
    if not hub_url_ok(url) or not token:
        sys.exit("linkd: set DOG_HUB_URL (wss://...) and DOG_DEVICE_TOKEN")
    link = Link(url, token, os.environ.get("DOG_DEVICE", "dog-01"), args.assume_synced)
    # SIGTERM cancels the loop, so the session cleanup disarms the dog as for any link loss.
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await link.run()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("DOG_LOG", "INFO"), format="%(name)s %(levelname)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(sys.argv[1:]))
