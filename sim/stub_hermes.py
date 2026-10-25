"""stub_hermes: a stand-in for Hermes Agent and the OpenAI speech endpoints, for tests/test_link.py.

It fakes the Hermes runs API (POST /v1/runs, the SSE stream of GET
/v1/runs/{id}/events, POST /v1/runs/{id}/stop) and the two OpenAI endpoints of
the hub (/v1/audio/transcriptions, /v1/audio/speech). When a run input contains
"bark", the stream calls do_trick("bark") on the hub's /tools with a real Model
Context Protocol client, in the middle of the stream, as Hermes does.
GET /_stub/requests returns each recorded request; POST /_stub/config changes config.
"""

import argparse
import time

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

#: What a test can change: STT text, reply deltas, delays (s), event stream status, raw SSE lines, TTS length (s).
config = {"transcript": "hello dog", "reply": ["Sure,", " I can do that.", " Here I go!"], "delta_delay": 0.05,
          "create_delay": 0, "events_status": 200, "junk_lines": [], "speech_seconds": 0.1, "tools_url": ""}
requests = []


def record(path, **fields):
    """Keep one request for the test; t_ns is monotonic, the clock of the fake bridge log."""
    requests.append({"t_ns": time.monotonic_ns(), "path": path, **fields})


async def set_config(request):
    """Change config values."""
    config.update(await request.json())
    return Response()


async def get_requests(request):
    """Return each recorded request, oldest first."""
    return JSONResponse(requests)


async def transcriptions(request):
    """Return the configured transcript for any audio file."""
    form = await request.form()
    audio = await form["file"].read()
    record("/v1/audio/transcriptions", model=form.get("model"), wav=audio[:4] == b"RIFF",
           authorization=request.headers.get("authorization"))
    return JSONResponse({"text": config["transcript"]})


async def speech(request):
    """Return speech_seconds of silent 24 kHz PCM16 for any text."""
    record("/v1/audio/speech", body=await request.json())
    return Response(content=b"\x00\x00" * int(24000 * config["speech_seconds"]), media_type="audio/pcm")


app = Starlette(routes=[
    Route("/_stub/config", set_config, methods=["POST"]),
    Route("/_stub/requests", get_requests),
    Route("/v1/audio/transcriptions", transcriptions, methods=["POST"]),
    Route("/v1/audio/speech", speech, methods=["POST"]),
])


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
