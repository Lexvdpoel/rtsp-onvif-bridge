"""A live view a browser can actually show.

No browser plays RTSP, and the two ways to get video into one both cost more
than they are worth here. HLS needs a JavaScript player, because only Safari
plays it natively -- and this page has no external scripts, which is what lets
it work on a machine with no internet. WebRTC needs signalling and a spread of
UDP ports through whatever sits between the browser and the camera.

MJPEG needs neither. An <img> tag pointed at a multipart stream plays it, in
every browser, with no library at all. The cost is bandwidth and an encoder per
viewer, which is why the frame rate is low and the stream stops the moment
nobody is watching.

ffmpeg runs here rather than on the camera: the controller can already reach
every camera's relay, and going through it means the browser needs a session
cookie rather than the camera's own credentials.
"""

from __future__ import annotations

import os
import shutil
import subprocess

BOUNDARY = "frameboundary"
CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={BOUNDARY}"

# Bounds, not preferences. A grid of a dozen cameras at a dozen frames a second
# would be a dozen encoders on one host, and the first thing to suffer would be
# the recording everything else exists for.
MIN_FPS, MAX_FPS = 1, 15
MIN_WIDTH, MAX_WIDTH = 160, 1920

_JPEG_START = b"\xff\xd8"
_JPEG_END = b"\xff\xd9"


def clamp_fps(value) -> int:
    try:
        return max(MIN_FPS, min(MAX_FPS, int(value)))
    except (TypeError, ValueError):
        return 6


def clamp_width(value) -> int:
    try:
        return max(MIN_WIDTH, min(MAX_WIDTH, int(value)))
    except (TypeError, ValueError):
        return 640


def command(source_url: str, fps: int, width: int, quality: int = 7) -> list[str]:
    """ffmpeg reading RTSP and writing a stream of JPEGs to stdout."""
    return [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        # Without this a slow consumer makes ffmpeg buffer rather than drop, and
        # the picture drifts further behind real time the longer it is watched.
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-i", source_url,
        "-an",
        "-vf", f"fps={fps},scale={width}:-2",
        "-q:v", str(quality),
        "-f", "mpjpeg",
        "-boundary_tag", BOUNDARY,
        "-",
    ]


def available() -> bool:
    return shutil.which("ffmpeg") is not None


def frames(source_url: str, fps: int = 6, width: int = 640):
    """Yield multipart chunks until the client goes away.

    ffmpeg's own mpjpeg muxer writes the boundaries and headers, so this only
    has to move bytes and make sure the process dies with the connection. A
    viewer who closes the tab must not leave an encoder running for ever.
    """
    proc = subprocess.Popen(
        command(source_url, fps, width),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        while True:
            chunk = proc.stdout.read(32768)
            if not chunk:
                return
            yield chunk
    finally:
        # Reached when the generator is closed, which Starlette does as soon as
        # the client disconnects.
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - it is going away either way
            pass
        if proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass


def still_frame(source_url: str, width: int = 640) -> bytes:
    """One JPEG, for the poster a grid tile shows before its stream starts."""
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", source_url,
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        "-q:v", "6",
        "-f", "image2", "-",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True,
            timeout=float(os.environ.get("LIVE_STILL_TIMEOUT", "15")),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return b""
    return proc.stdout if proc.returncode == 0 else b""
