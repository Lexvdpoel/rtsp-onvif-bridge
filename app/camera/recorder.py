"""Saves a still for every detection, into the shared state volume.

A detection that leaves nothing behind can only ever be a number on a card. A
still turns it into something you can check: was that a person or a bin bag, and
is the threshold set anywhere near right.

The still is *the frame the detector looked at*, encoded, rather than a fresh
grab from the camera. That is not an optimisation, it is the only way the
outline can be right. Opening a second stream and pulling a frame takes a second
or two, and in that time a walking person has walked -- the picture was a later
moment than the box, so the box trailed behind them. The frame and the box have
to be the same instant or neither can be trusted.

Writing happens on its own thread. Encoding means running ffmpeg, and the
detector must not wait for it: a detector that blocks on disk stops detecting.
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


def even(value: int) -> int:
    """Encoders reject odd dimensions, and a pixel either way is nothing."""
    return max(2, int(value) // 2 * 2)


def encode(frame_bytes: bytes, size: int, out_width: int, out_height: int) -> bytes:
    """One JPEG from a raw RGB frame, back in its proper proportions.

    The detector decodes to a square because a fixed frame size is what lets it
    read whole frames off a pipe without guessing. Scaling back here undoes that
    squash -- and because it is a uniform scale, the box's coordinates, which are
    fractions of the frame, still land exactly where they did.
    """
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size}x{size}",
        "-i", "-",
        "-vf", f"scale={out_width}:{out_height}",
        "-q:v", str(QUALITY),
        "-frames:v", "1",
        "-f", "image2", "-",
    ]
    try:
        proc = subprocess.run(cmd, input=frame_bytes, capture_output=True,
                              timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return b""
    except FileNotFoundError:
        return b""
    return proc.stdout if proc.returncode == 0 else b""


class Recorder(threading.Thread):
    """Takes detections off a queue and writes one still each."""

    def __init__(self, cam_id: str, state_dir: str, frame_size: int,
                 aspect: float = 16 / 9, depth: int = 4):
        super().__init__(name="recorder", daemon=True)
        self.cam_id = cam_id
        self.state_dir = state_dir
        self.frame_size = frame_size
        self.out_width = even(WIDTH)
        self.out_height = even(round(WIDTH / (aspect or 16 / 9)))
        # Bounded, and shallower than it was now that each item carries a frame:
        # at a megabyte or so each, a backlog is memory the camera has to find.
        # If encoding falls behind, the detector keeps running and the backlog is
        # dropped rather than growing until the container runs out.
        self._queue: queue.Queue = queue.Queue(maxsize=depth)
        self._stop = threading.Event()
        self.written = 0
        self.dropped = 0
        self.error = ""

    def record(self, object_type: str, score: float, box, frame,
               at: float | None = None):
        """Called from the detector. Never blocks, never raises."""
        try:
            self._queue.put_nowait((object_type, score, box, frame, at or time.time()))
        except queue.Full:
            self.dropped += 1
            if self.dropped in (1, 10) or self.dropped % 100 == 0:
                print(
                    f"[clips] still not saved: the last {self.dropped} were "
                    "dropped because encoding cannot keep up with detections. "
                    "The events themselves are unaffected."
                )

    def run(self):
        while not self._stop.is_set():
            try:
                object_type, score, box, frame, at = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._write(object_type, score, box, frame, at)
            except Exception as exc:  # noqa: BLE001 - one failure is not fatal
                self.error = str(exc)[:200]
                print(f"[clips] could not save a still: {exc}")

    def _write(self, object_type: str, score: float, box, frame, at: float):
        image = encode(frame.tobytes(), self.frame_size,
                       self.out_width, self.out_height)
        if not image:
            self.error = "ffmpeg returned no image"
            return
        directory = os.path.join(
            clips.camera_dir(self.state_dir, self.cam_id), clips.day_of(at)
        )
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, clips.filename(at, object_type, score, box))
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
