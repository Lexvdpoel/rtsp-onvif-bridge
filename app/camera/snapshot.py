"""JPEG snapshots, pulled from the stream with ffmpeg and cached briefly.

ONVIF clients poll the snapshot URI far more often than a new frame is worth
fetching, so results are cached for a few seconds and only one ffmpeg run is
allowed at a time.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

CACHE_SECONDS = float(os.environ.get("SNAPSHOT_CACHE_SECONDS", "3"))
TIMEOUT_SECONDS = float(os.environ.get("SNAPSHOT_TIMEOUT", "15"))

_lock = threading.Lock()
_cache: dict[str, tuple[float, bytes]] = {}


def grab(source_url: str, transport: str = "tcp") -> bytes:
    """Return a JPEG for the given stream URL, or b'' if it could not be read."""
    now = time.time()
    cached = _cache.get(source_url)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]

    with _lock:
        # Another thread may have refreshed the cache while we waited.
        cached = _cache.get(source_url)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]

        image = _run_ffmpeg(source_url, transport)
        if image:
            _cache[source_url] = (time.time(), image)
        elif cached:
            return cached[1]  # serve the stale frame rather than nothing
        return image


def _run_ffmpeg(source_url: str, transport: str) -> bytes:
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
    ]
    if source_url.startswith("rtsp"):
        cmd += ["-rtsp_transport", transport or "tcp"]
    cmd += [
        "-i", source_url,
        "-frames:v", "1",
        "-q:v", "4",
        "-f", "image2",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print("[snapshot] ffmpeg timed out")
        return b""
    except FileNotFoundError:
        print("[snapshot] ffmpeg not installed")
        return b""

    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", "replace").strip()[:300]
        print(f"[snapshot] ffmpeg failed: {err}")
        return b""
    return proc.stdout
