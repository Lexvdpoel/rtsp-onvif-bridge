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


def frames(source_url: str, fps: int = 6, width: int = 640, label: str = ""):
    """Yield multipart chunks until the client goes away.

    ffmpeg's own mpjpeg muxer writes the boundaries and headers, so this only
    has to move bytes and make sure the process dies with the connection. A
    viewer who closes the tab must not leave an encoder running for ever.

    ffmpeg's stderr is kept rather than discarded. A stream that cannot be
    opened still answers 200 -- the headers go out before the first frame is
    asked for -- so without the error the tile says "connecting" for ever and
    nothing anywhere says why.
    """
    proc = subprocess.Popen(
        command(source_url, fps, width),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    who = label or source_url
    sent = 0
    try:
        while True:
            chunk = proc.stdout.read(32768)
            if not chunk:
                if not sent:
                    _report_failure(who, proc)
                return
            sent += len(chunk)
            yield chunk
    finally:
        # Reached when the generator is closed, which Starlette does as soon as
        # the client disconnects.
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - it is going away either way
            pass
        for stream in (proc.stdout, proc.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass


def _report_failure(who: str, proc: subprocess.Popen):
    """Say why a stream produced nothing, in the words ffmpeg used."""
    detail = ""
    try:
        detail = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        pass
    first = detail.splitlines()[0] if detail else "no output and no error"
    print(f"[live] no picture from {who}: {first}")
    if "Connection timed out" in detail or "No route to host" in detail:
        # Worth spelling out: the cameras are on a macvlan network, and a
        # container that is not on it cannot reach them however right the
        # address looks. Nothing in the address or the log would hint at it.
        print(
            "[live] the controller cannot reach this camera over the network. "
            "Cameras sit on the macvlan network, which a container on Docker's "
            "bridge cannot route to. See LIVE_VIEW in the README."
        )


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
