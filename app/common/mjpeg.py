"""A live view a browser can show, carried through the shared volume.

No browser plays RTSP, and the two ways to get real video into one both cost
more than they are worth here. HLS needs a JavaScript player, because only
Safari plays it natively -- and this page has no external scripts, which is what
lets it work on a machine with no internet. WebRTC needs signalling and a spread
of UDP ports.

MJPEG needs neither: an <img> pointed at a multipart stream plays it everywhere,
with no library at all.

Getting the frames to the controller is the harder half. The cameras sit on a
macvlan network, and a container on Docker's bridge cannot route to macvlan
children on the same host -- traffic leaves by the physical interface and never
comes back. So the controller does not read the cameras over the network at all.
It asks for a live view by touching a file in the volume both already share, the
camera notices and starts writing frames there, and the controller reads them
and hands them to the browser. No route between the two is needed, and nothing
in the browser ever talks to a camera directly.

Frames are written one at a time and moved into place, so a reader sees a whole
picture or the previous one, never half of each. That matters more than it
sounds on a share where the two processes have no lock between them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

BOUNDARY = "frameboundary"
CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={BOUNDARY}"

# Bounds, not preferences. A grid of a dozen cameras at a dozen frames a second
# would be a dozen encoders on one host, and the first thing to suffer would be
# the recording everything else exists for.
MIN_FPS, MAX_FPS = 1, 15
MIN_WIDTH, MAX_WIDTH = 160, 1920

# How long a request to be watched stays good for. The controller renews it
# while a browser is connected; when it stops renewing, the camera stops
# encoding. Long enough to ride out a slow poll, short enough that a closed tab
# does not keep an encoder alive for long.
DEMAND_SECONDS = 6.0

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


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


# ---------------------------------------------------------------- the handshake


def live_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "live")


def demand_path(state_dir: str, cam_id: str) -> str:
    return os.path.join(live_dir(state_dir), f"{cam_id}.want")


def frame_path(state_dir: str, cam_id: str) -> str:
    return os.path.join(live_dir(state_dir), f"{cam_id}.jpg")


def ask_for(state_dir: str, cam_id: str, viewer: str, quality: str,
            fps: int, width: int):
    """Tell the camera this viewer is watching, and what it wants.

    One file per viewer rather than one per camera. They used to share a single
    file, and two viewers wanting different things overwrote each other several
    times a second -- the camera saw the quality change, restarted its encoder,
    saw it change back, and the picture dropped to the other stream and back
    again for as long as both were open.
    """
    os.makedirs(live_dir(state_dir), exist_ok=True)
    path = demand_path(state_dir, cam_id) + "." + _safe_viewer(viewer)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as fh:
            fh.write(f"{quality} {clamp_fps(fps)} {clamp_width(width)}")
        os.replace(tmp, path)
    except OSError:
        pass


def stop_asking(state_dir: str, cam_id: str, viewer: str):
    """Withdraw one viewer's request, rather than waiting for it to go stale."""
    try:
        os.remove(demand_path(state_dir, cam_id) + "." + _safe_viewer(viewer))
    except OSError:
        pass


def _safe_viewer(viewer: str) -> str:
    keep = "".join(ch for ch in str(viewer) if ch.isalnum())
    return keep[:32] or "anon"


def wanted(state_dir: str, cam_id: str) -> dict | None:
    """The most anyone is asking for, or None when nobody is watching.

    The union rather than the last writer: with two people watching the same
    camera at different qualities, serving the lower one would mean the person
    who asked for detail does not get it. Encoding once at the higher setting
    costs no more than encoding once at the lower one.
    """
    prefix = os.path.basename(demand_path(state_dir, cam_id)) + "."
    directory = live_dir(state_dir)
    now = time.time()
    quality, fps, width = "", 0, 0
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in names:
        if not name.startswith(prefix) or name.endswith(".tmp"):
            continue
        path = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(path) > DEMAND_SECONDS:
                # Whoever asked has gone; tidy up rather than re-reading it for
                # ever.
                os.remove(path)
                continue
            with open(path) as fh:
                asked, asked_fps, asked_width = fh.read().split()
        except (OSError, ValueError):
            continue
        if asked == "high":
            quality = "high"
        elif not quality:
            quality = asked
        fps = max(fps, clamp_fps(asked_fps))
        width = max(width, clamp_width(asked_width))
    if not fps:
        return None
    return {"quality": quality or "low", "fps": fps, "width": width}


# ------------------------------------------------------------------- encoding


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
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-",
    ]


def available() -> bool:
    return shutil.which("ffmpeg") is not None


def split_frames(stream, chunk_size: int = 32768):
    """Yield complete JPEGs from a byte stream of them, one at a time."""
    buffer = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return
        buffer += chunk
        while True:
            start = buffer.find(JPEG_START)
            if start < 0:
                # Nothing usable yet; do not let junk accumulate for ever.
                if len(buffer) > 4 * 1024 * 1024:
                    buffer = b""
                break
            end = buffer.find(JPEG_END, start + 2)
            if end < 0:
                buffer = buffer[start:]
                break
            yield buffer[start:end + 2]
            buffer = buffer[end + 2:]


# -------------------------------------------------------------- the multipart


def part(jpeg: bytes) -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        "Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(jpeg)}\r\n\r\n"
    ).encode() + jpeg + b"\r\n"


def stream_from_file(state_dir: str, cam_id: str, quality: str, fps: int,
                     width: int, idle_timeout: float = 20.0):
    """Multipart chunks read from whatever the camera is writing.

    Renews this viewer's request on the way round, so the camera keeps encoding
    for exactly as long as a browser is attached, and withdraws it on the way
    out rather than leaving it to go stale.
    """
    viewer = uuid.uuid4().hex[:12]
    path = frame_path(state_dir, cam_id)
    interval = 1.0 / max(1, clamp_fps(fps))
    last_stamp = 0.0
    last_new = time.monotonic()
    try:
        while True:
            ask_for(state_dir, cam_id, viewer, quality, fps, width)
            try:
                stamp = os.path.getmtime(path)
            except OSError:
                stamp = 0.0
            if stamp and stamp != last_stamp:
                try:
                    with open(path, "rb") as fh:
                        jpeg = fh.read()
                except OSError:
                    jpeg = b""
                if jpeg.startswith(JPEG_START):
                    last_stamp = stamp
                    last_new = time.monotonic()
                    yield part(jpeg)
            elif time.monotonic() - last_new > idle_timeout:
                # Nothing has arrived for long enough that something is wrong.
                # Ending the response is what makes the tile say so.
                return
            time.sleep(interval)
    finally:
        # Reached when the browser goes away, which is the moment the camera
        # should stop encoding for it rather than a few seconds later.
        stop_asking(state_dir, cam_id, viewer)
