"""stub_hermes: a stand-in for Hermes Agent and the OpenAI speech endpoints, for tests/test_link.py.

It fakes the Hermes runs API (POST /v1/runs, the SSE stream of GET
/v1/runs/{id}/events, POST /v1/runs/{id}/stop) and the two OpenAI endpoints of
the hub (/v1/audio/transcriptions, /v1/audio/speech). When a run input contains
"bark", the stream calls do_trick("bark") on the hub's /tools with a real Model
Context Protocol client, in the middle of the stream, as Hermes does.
GET /_stub/requests returns each recorded request; POST /_stub/config changes config.
"""

import argparse
import asyncio
import contextlib
import json
import os
import time
import uuid

import httpx2
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

#: What a test can change: STT text, reply deltas, delays (s), event stream status, raw SSE lines, TTS length (s).
config = {"transcript": "hello dog", "reply": ["Sure,", " I can do that.", " Here I go!"], "delta_delay": 0.05,
          "create_delay": 0, "events_status": 200, "junk_lines": [], "speech_seconds": 0.1, "tools_url": ""}
TOOLS_TOKEN = os.environ.get("TOOLS_TOKEN", "")
requests = []
#: Run id to (input text, stop event).
runs = {}
app = FastAPI()


def record(path, **fields):
    """Keep one request for the test; t_ns is monotonic, the clock of the fake bridge log."""
    requests.append({"t_ns": time.monotonic_ns(), "path": path, **fields})


def hermes_headers(request):
    """Return the request headers that the hub must send to Hermes."""
    return {name: request.headers.get(name) for name in ("authorization", "idempotency-key", "x-hermes-session-key")}


@app.post("/_stub/config")
async def set_config(request: Request):
    """Change config values."""
    config.update(await request.json())


@app.get("/_stub/requests")
async def get_requests():
    """Return each recorded request, oldest first."""
    return requests


@app.post("/v1/runs", status_code=202)
async def create_run(request: Request):
    """Start a run, as Hermes does: 202 with a run id, after create_delay seconds."""
    body = await request.json()
    run_id = f"run_{uuid.uuid4().hex}"
    runs[run_id] = (body["input"], asyncio.Event())
    record("/v1/runs", headers=hermes_headers(request), body=body, run_id=run_id)
    await asyncio.sleep(config["create_delay"])
    return {"run_id": run_id, "status": "started"}


@app.post("/v1/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request):
    """Stop a run; its stream ends with run.cancelled."""
    record("/v1/runs/stop", headers=hermes_headers(request), run_id=run_id)
    runs[run_id][1].set()
    return {"run_id": run_id, "status": "stopping"}


@app.get("/v1/runs/{run_id}/events")
async def run_events(run_id: str):
    """Stream the run events in the Hermes SSE format, or answer with events_status."""
    record("/v1/runs/events", run_id=run_id)
    if config["events_status"] != 200:
        return Response(status_code=config["events_status"])
    return StreamingResponse(_events(*runs[run_id]), media_type="text/event-stream")


def _frame(name, **fields):
    """Return one SSE event: a data line with the event JSON, then a blank line."""
    return f"data: {json.dumps({'event': name, **fields})}\n\n"


async def _events(text, stopped):
    """Yield the SSE lines of one run: deltas, a tool call after the first delta for "bark", then the end event."""
    yield ": open\n\n"
    for line in config["junk_lines"]:
        yield f"{line}\n\n"
    for index, delta in enumerate(config["reply"]):
        if index == 1 and "bark" in text:
            yield _frame("tool.started", tool="mcp_dog_do_trick")
            await call_tool("do_trick", {"name": "bark"})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopped.wait(), config["delta_delay"])
        if stopped.is_set():
            yield _frame("run.cancelled")
            return
        yield _frame("message.delta", delta=delta)
    yield _frame("run.completed", output="".join(config["reply"]))


async def call_tool(name, arguments):
    """Call one hub tool as a Model Context Protocol client and record the result."""
    headers = {"Authorization": f"Bearer {TOOLS_TOKEN}"}
    async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(10, read=60)) as http:
        async with Client(streamable_http_client(config["tools_url"], http_client=http)) as client:
            result = await client.call_tool(name, arguments)
    text = " ".join(getattr(block, "text", "") for block in result.content)
    record(f"tool:{name}", is_error=result.is_error, text=text)


@app.post("/v1/audio/transcriptions")
async def transcriptions(request: Request):
    """Return the configured transcript for any audio file."""
    form = await request.form()
    audio = await form["file"].read()
    record("/v1/audio/transcriptions", model=form.get("model"), wav=audio[:4] == b"RIFF",
           authorization=request.headers.get("authorization"))
    return {"text": config["transcript"]}


@app.post("/v1/audio/speech")
async def speech(request: Request):
    """Return speech_seconds of silent 24 kHz PCM16 for any text."""
    record("/v1/audio/speech", body=await request.json())
    return Response(content=b"\x00\x00" * int(24000 * config["speech_seconds"]), media_type="audio/pcm")


def main():
    """Serve the stub on loopback; the hub's /tools token comes from TOOLS_TOKEN."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--tools-url", default="http://127.0.0.1:8000/tools/")
    args = parser.parse_args()
    config["tools_url"] = args.tools_url
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
