"""Stream mic + system-output audio to Deepgram nova-3, printing transcripts."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import urllib.parse
from itertools import groupby
from typing import Any

import numpy as np
import websockets

SAMPLE_RATE = 16000
CAPTURE_CHANNELS = 1  # each pw-record subprocess captures mono
DEEPGRAM_CHANNELS = 2  # we interleave mic+system into a stereo stream
BLOCK_SIZE = 1600  # 100 ms @ 16 kHz
CHUNK_BYTES = BLOCK_SIZE * 2  # int16
QUEUE_MAX = 50  # ~5 s buffer; drop oldest on full so producers stay live

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"

# Created by the home-manager module's PipeWire drop-in. When present, we
# default to this mic so the tool picks up the AEC'd signal automatically.
ECHO_CANCEL_SOURCE = "echo-cancel-source"


def default_sink() -> str:
    try:
        out = subprocess.check_output(
            ["pw-metadata", "0", "default.audio.sink"], text=True, timeout=5
        )
    except FileNotFoundError:
        raise SystemExit("pw-metadata not found; is PipeWire installed?") from None
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"pw-metadata failed: {e}") from None
    except subprocess.TimeoutExpired:
        raise SystemExit("pw-metadata timed out") from None
    m = re.search(r"value:'([^']*)'", out)
    if not m:
        raise SystemExit(f"could not parse default sink:\n{out}")
    return json.loads(m.group(1))["name"]


async def spawn_camera_keeper(device: str) -> asyncio.subprocess.Process:
    # Combo webcams (e.g. OBSBOT) only ship USB audio while their video stream
    # is active; streaming frames to /dev/null keeps the mic route alive.
    return await asyncio.create_subprocess_exec(
        "v4l2-ctl",
        "--stream-mmap",
        "--stream-count=0",
        f"--device={device}",
        "--stream-to=/dev/null",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def spawn_capture(
    target: str | None, capture_sink: bool
) -> asyncio.subprocess.Process:
    args = [
        "pw-record",
        f"--rate={SAMPLE_RATE}",
        f"--channels={CAPTURE_CHANNELS}",
        "--format=s16",
        "--raw",
    ]
    if target:
        args.append(f"--target={target}")
    if capture_sink:
        args.extend(["-P", "stream.capture.sink=true"])
    args.append("-")
    return await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def read_chunks(
    proc: asyncio.subprocess.Process,
    q: asyncio.Queue[np.ndarray],
    label: str,
    stats: dict[str, int],
) -> None:
    assert proc.stdout is not None
    try:
        while True:
            data = await proc.stdout.readexactly(CHUNK_BYTES)
            stats[label] += len(data)
            chunk = np.frombuffer(data, dtype=np.int16)
            if q.full():
                q.get_nowait()
            q.put_nowait(chunk)
    except asyncio.IncompleteReadError:
        print(f"[{label}] pw-record exited (rc={proc.returncode})", file=sys.stderr)


async def watchdog(stats: dict[str, int], delay: float = 3.0) -> None:
    hints = {
        "mic": (
            "try `--list-sources`, then `--mic <name>` "
            "(combo webcams like OBSBOT need `--keep-camera /dev/videoN`)"
        ),
        "system": "nothing is playing, or pass `--system <sink-name>`",
    }
    await asyncio.sleep(delay)
    for label, n in stats.items():
        if n == 0:
            print(
                f"[warn] no audio from {label} after {delay:.0f}s — {hints[label]}",
                file=sys.stderr,
            )


def emit_transcript(data: dict[str, Any]) -> None:
    channel_idx = data["channel_index"][0]
    alt = data["channel"]["alternatives"][0]
    words = alt.get("words") or []
    if not words:
        text = alt.get("transcript", "")
        if text:
            label = "me" if channel_idx == 0 else "remote"
            print(f"[{label}] {text}", flush=True)
        return
    for sp, ws in groupby(words, key=lambda w: int(w.get("speaker", 0))):
        label = "me" if channel_idx == 0 else f"remote-{sp}"
        toks = [w.get("punctuated_word") or w.get("word", "") for w in ws]
        print(f"[{label}] {' '.join(toks)}", flush=True)


def pw_nodes() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    try:
        out = subprocess.check_output(["pw-dump"], text=True, timeout=5)
    except FileNotFoundError:
        return []
    except subprocess.CalledProcessError as e:
        print(
            f"[warn] pw-dump failed ({e}); skipping node enumeration",
            file=sys.stderr,
        )
        return []
    except subprocess.TimeoutExpired:
        print("[warn] pw-dump timed out; skipping node enumeration", file=sys.stderr)
        return []
    return [
        (obj, (obj.get("info") or {}).get("props") or {})
        for obj in json.loads(out)
        if obj.get("type") == "PipeWire:Interface:Node"
    ]


def source_exists(name: str) -> bool:
    return any(
        props.get("media.class") == "Audio/Source" and props.get("node.name") == name
        for _, props in pw_nodes()
    )


def list_sources() -> None:
    for _, props in pw_nodes():
        mc = props.get("media.class")
        if mc not in ("Audio/Source", "Audio/Sink"):
            continue
        name = props.get("node.name", "")
        desc = props.get("node.description", "")
        print(f"{mc:12s}  {name}")
        if desc and desc != name:
            print(f"              ({desc})")


async def run(
    mic_target: str | None,
    sink_target: str,
    api_key: str,
    keep_camera: str | None,
) -> None:
    mic_q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=QUEUE_MAX)
    sys_q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=QUEUE_MAX)
    stats = {"mic": 0, "system": 0}

    camera_proc: asyncio.subprocess.Process | None = None
    if keep_camera:
        camera_proc = await spawn_camera_keeper(keep_camera)
        print(
            f"[camera] streaming {keep_camera} -> /dev/null to keep device awake",
            file=sys.stderr,
        )
        # Let PipeWire register the now-active audio route before we open it.
        await asyncio.sleep(0.5)

    mic_proc = await spawn_capture(mic_target, capture_sink=False)
    sys_proc = await spawn_capture(sink_target, capture_sink=True)

    print(f"[mic]    target={mic_target or '<default source>'}", file=sys.stderr)
    print(f"[system] target={sink_target} (sink monitor)", file=sys.stderr)

    query = urllib.parse.urlencode(
        {
            "model": "nova-3",
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "channels": DEEPGRAM_CHANNELS,
            "multichannel": "true",
            "diarize": "true",
            "smart_format": "true",
            "interim_results": "false",
        }
    )
    uri = f"{DEEPGRAM_URL}?{query}"
    headers = {"Authorization": f"Token {api_key}"}

    try:
        async with websockets.connect(uri, additional_headers=headers) as ws:

            async def receiver() -> None:
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("is_final"):
                        emit_transcript(data)

            recv_task = asyncio.create_task(receiver())
            mic_reader = asyncio.create_task(
                read_chunks(mic_proc, mic_q, "mic", stats)
            )
            sys_reader = asyncio.create_task(
                read_chunks(sys_proc, sys_q, "system", stats)
            )
            watch_task = asyncio.create_task(watchdog(stats))

            loop = asyncio.get_running_loop()
            interval = BLOCK_SIZE / SAMPLE_RATE
            next_tick = loop.time()
            empty = np.zeros(BLOCK_SIZE, dtype=np.int16)
            try:
                while True:
                    next_tick += interval
                    delay = next_tick - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    elif delay < -interval:
                        # Fell >1 tick behind (GC pause, suspend, WS stall);
                        # skip the backlog rather than burst-send to Deepgram.
                        next_tick = loop.time() + interval
                    try:
                        mic_chunk = mic_q.get_nowait()
                    except asyncio.QueueEmpty:
                        mic_chunk = empty
                    try:
                        sys_chunk = sys_q.get_nowait()
                    except asyncio.QueueEmpty:
                        sys_chunk = empty
                    # ch0 = mic (you), ch1 = system (remotes) — emit_transcript
                    # relies on this mapping via channel_index.
                    stereo = np.stack([mic_chunk, sys_chunk], axis=-1).ravel()
                    await ws.send(stereo.tobytes())
            finally:
                try:
                    await ws.send(json.dumps({"type": "CloseStream"}))
                except websockets.ConnectionClosed:
                    pass
                for t in (recv_task, mic_reader, sys_reader, watch_task):
                    t.cancel()
    finally:
        procs = [mic_proc, sys_proc]
        if camera_proc is not None:
            procs.append(camera_proc)
        for p in procs:
            if p.returncode is None:
                p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), timeout=2)
                except TimeoutError:
                    p.kill()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Stream mic + system-output audio to Deepgram nova-3; "
            "print transcript to stdout. "
            "Requires DEEPGRAM_API_KEY and PipeWire (uses pw-record)."
        )
    )
    parser.add_argument(
        "--mic",
        help=(
            "PipeWire node name or serial for the mic "
            "(default: echo-cancel-source if present, else the default source)"
        ),
    )
    parser.add_argument(
        "--system",
        help=(
            "PipeWire sink name whose monitor to capture "
            "(default: current default sink)"
        ),
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="list PipeWire audio sources and sinks, then exit",
    )
    parser.add_argument(
        "--keep-camera",
        metavar="DEVICE",
        help=(
            "keep a V4L2 camera (e.g. /dev/video0) active for the duration "
            "of the run. Use for combo webcams like OBSBOT whose USB audio "
            "only flows while video is open."
        ),
    )
    args = parser.parse_args()

    if args.list_sources:
        list_sources()
        return

    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        raise SystemExit("DEEPGRAM_API_KEY is not set")

    sink = args.system or default_sink()
    mic = args.mic or (
        ECHO_CANCEL_SOURCE if source_exists(ECHO_CANCEL_SOURCE) else None
    )

    try:
        asyncio.run(run(mic, sink, api_key, args.keep_camera))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
