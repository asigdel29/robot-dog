"""stub_hermes: a stand-in for Hermes Agent and the OpenAI speech endpoints, for tests and the dev stack.

It fakes the Hermes runs API (POST /v1/runs, the GET /v1/runs/{id}/events SSE
stream in the Hermes wire format, POST /v1/runs/{id}/stop) and the two OpenAI
endpoints that the hub uses (/v1/audio/transcriptions, /v1/audio/speech). When
a run input contains "bark", the stream calls do_trick("bark") on the hub's
/tools endpoint with a real Model Context Protocol client, in the middle of the
stream, as Hermes does. Each request is recorded: GET /_stub/requests returns
the list, and POST /_stub/config sets the transcript, the reply and the delays.
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

#: Text that a test can change with POST /_stub/config.
config = {"transcript": "hello dog", "reply": ["Sure,", " I can do that.", " Here I go!"], "delta_delay": 0.05,
          "create_delay": 0, "tools_url": "", "tools_token": ""}
requests = []
runs = {}
app = FastAPI()


def record(path, **fields):
    """Keep one request for the test to read; t_ns is monotonic, the clock of the fake bridge log."""
    requests.append({"t_ns": time.monotonic_ns(), "path": path, **fields})


def hermes_headers(request):
    """Return the request headers that the hub must send to Hermes."""
    return {name: request.headers.get(name) for name in ("authorization", "idempotency-key", "x-hermes-session-key")}


@app.post("/_stub/config")
async def set_config(request: Request):
    """Change the transcript, the reply deltas, the delay between deltas or the delay of the create reply."""
    config.update(await request.json())
    return config


@app.get("/_stub/requests")
async def get_requests():
    """Return each recorded request, oldest first."""
    return requests


@app.post("/v1/runs", status_code=202)
async def create_run(request: Request):
    """Start a run, as Hermes does: 202 with a run id, after create_delay seconds."""
    body = await request.json()
    run_id = f"run_{uuid.uuid4().hex}"
    runs[run_id] = {"input": body.get("input", ""), "stopped": asyncio.Event()}
    record("/v1/runs", headers=hermes_headers(request), body=body, run_id=run_id)
    await asyncio.sleep(config["create_delay"])
    return {"run_id": run_id, "status": "started", "replayed": False}


@app.post("/v1/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request):
    """Stop a run; its stream ends with run.cancelled."""
    record("/v1/runs/stop", headers=hermes_headers(request), run_id=run_id)
    if run_id not in runs:
        return Response(status_code=404)
    runs[run_id]["stopped"].set()
    return {"run_id": run_id, "status": "stopping"}


@app.get("/v1/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    """Stream the run events in the Hermes SSE format."""
    record("/v1/runs/events", headers=hermes_headers(request), run_id=run_id)
    if run_id not in runs:
        return Response(status_code=404)
    return StreamingResponse(_events(run_id), media_type="text/event-stream")


async def _events(run_id):
    """Yield the SSE frames of one run: deltas, an optional mid-stream tool call, then a terminal event."""
    run = runs[run_id]
    seq = 0

    def frame(name, **fields):
        nonlocal seq
        seq += 1
        payload = {"event": name, "run_id": run_id, "timestamp": time.time(), **fields, "seq": seq}
        return f"id: {seq}\ndata: {json.dumps(payload)}\n\n"

    yield ": open\n\n"
    text = ""
    for index, delta in enumerate(config["reply"]):
        if index == 1 and "bark" in run["input"].lower():
            yield frame("tool.started", tool="mcp_dog_do_trick", preview='{"name": "bark"}')
            started = time.monotonic()
            result = await call_tool("do_trick", {"name": "bark"})
            yield frame("tool.completed", tool="mcp_dog_do_trick", duration=time.monotonic() - started,
                        error=result["is_error"], preview=result["text"][:500])
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(run["stopped"].wait(), config["delta_delay"])
        if run["stopped"].is_set():
            yield frame("run.cancelled", completed=False, interrupted=True, turn_exit_reason="interrupted_by_user")
            yield ": stream closed\n\n"
            return
        text += delta
        yield frame("message.delta", delta=delta)
    yield frame("run.completed", completed=True, output=text, usage={}, runtime={})
    yield ": stream closed\n\n"


async def call_tool(name, arguments):
    """Call one hub tool as a Model Context Protocol client and record the result."""
    headers = {"Authorization": f"Bearer {config['tools_token']}"}
    async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(10, read=60)) as http:
        async with Client(streamable_http_client(config["tools_url"], http_client=http)) as client:
            result = await client.call_tool(name, arguments)
    text = " ".join(getattr(block, "text", "") for block in result.content)
    record(f"tool:{name}", arguments=arguments, is_error=result.is_error, text=text)
    return {"is_error": result.is_error, "text": text}


@app.post("/v1/audio/transcriptions")
async def transcriptions(request: Request):
    """Return the configured transcript for any audio file."""
    form = await request.form()
    audio = await form["file"].read()
    record("/v1/audio/transcriptions", model=form.get("model"), bytes=len(audio), wav=audio[:4] == b"RIFF",
           authorization=request.headers.get("authorization"))
    return {"text": config["transcript"]}


@app.post("/v1/audio/speech")
async def speech(request: Request):
    """Return 0.1 s of silent 24 kHz PCM16 for any text."""
    body = await request.json()
    record("/v1/audio/speech", body=body)
    return Response(content=b"\x00\x00" * 2400, media_type="audio/pcm")


def main():
    """Serve the stub on loopback."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--tools-url", default="http://127.0.0.1:8000/tools/")
    args = parser.parse_args()
    config["tools_url"] = args.tools_url
    config["tools_token"] = os.environ.get("TOOLS_TOKEN", "")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
