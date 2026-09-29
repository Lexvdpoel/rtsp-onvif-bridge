"""Saves a still for every detection, into the shared state volume.

A detection that leaves nothing behind can only ever be a number on a card. A
still turns it into something you can check: was that a person or a bin bag, and
is the threshold set anywhere near right.

Writing happens on its own thread. Grabbing a frame means running ffmpeg, which
takes a second or two, and the detector must not wait for it -- a detector that
blocks on disk stops detecting.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time

from ..common import clips

TIMEOUT_SECONDS = float(os.environ.get("CLIP_TIMEOUT", "15"))
# Stored stills are for review, not for evidence: a width of 640 keeps a face
# recognisable at a fraction of the bytes, which is what decides how many fit in
# the budget.
WIDTH = int(os.environ.get("CLIP_WIDTH", "640"))
QUALITY = int(os.environ.get("CLIP_QUALITY", "6"))


def grab(source_url: str, transport: str = "tcp") -> bytes:
    """One JPEG from the stream, scaled down, or b'' if it could not be read."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if source_url.startswith("rtsp"):
        cmd += ["-rtsp_transport", transport or "tcp"]
    cmd += [
        "-i", source_url,
        "-frames:v", "1",
        "-vf", f"scale={WIDTH}:-2",
        "-q:v", str(QUALITY),
        "-f", "image2",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return b""
    except FileNotFoundError:
        return b""
    return proc.stdout if proc.returncode == 0 else b""


class Recorder(threading.Thread):
    """Takes detections off a queue and writes one still each."""

    def __init__(self, cam_id: str, state_dir: str, source_url: str,
                 transport: str = "tcp", depth: int = 8):
        super().__init__(name="recorder", daemon=True)
        self.cam_id = cam_id
        self.state_dir = state_dir
        self.source_url = source_url
        self.transport = transport
        # Bounded on purpose. If grabbing falls behind -- a wedged camera, a
        # busy host -- the detector keeps running and the backlog is dropped,
        # rather than growing until the container runs out of memory.
        self._queue: queue.Queue = queue.Queue(maxsize=depth)
        self._stop = threading.Event()
        self.written = 0
        self.dropped = 0
        self.error = ""

    def record(self, object_type: str, score: float, at: float | None = None):
        """Called from the detector. Never blocks, never raises."""
        try:
            self._queue.put_nowait((object_type, score, at or time.time()))
        except queue.Full:
            self.dropped += 1
            if self.dropped in (1, 10) or self.dropped % 100 == 0:
                print(
                    f"[clips] still not saved: the last {self.dropped} were "
                    "dropped because grabbing frames cannot keep up with "
                    "detections. The events themselves are unaffected."
                )

    def run(self):
        while not self._stop.is_set():
            try:
                object_type, score, at = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._write(object_type, score, at)
            except Exception as exc:  # noqa: BLE001 - one failure is not fatal
                self.error = str(exc)[:200]
                print(f"[clips] could not save a still: {exc}")

    def _write(self, object_type: str, score: float, at: float):
        image = grab(self.source_url, self.transport)
        if not image:
            self.error = "ffmpeg returned no frame"
            return
        directory = os.path.join(
            clips.camera_dir(self.state_dir, self.cam_id), clips.day_of(at)
        )
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, clips.filename(at, object_type, score))
        # Written under a temporary name and moved into place, so the controller
        # never lists a file it is halfway through reading.
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(image)
        os.replace(tmp, path)
        self.written += 1
        self.error = ""

    def stop(self):
        self._stop.set()
