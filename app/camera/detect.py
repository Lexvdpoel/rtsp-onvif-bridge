"""Object detection on the camera's own stream.

Dumb cameras send pixels and nothing else, so the detection has to happen here.
It runs on the sub stream: a few frames a second is enough to say something is
there, and it keeps the cost per camera small enough to run several at once.

The model is YOLOX; see yolox.py for what it is and why. It names what it sees
-- a bus rather than a vehicle, a dog rather than an animal -- and every
detection carries both that name and the coarse type an NVR's event filter
understands, because the two readers want different things.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from . import classes
from .yolox import DEFAULT_MODEL, Model, ModelUnavailable  # noqa: F401

# With DETECT_DEBUG=1 every candidate the model returns is logged, including the
# ones below the confidence threshold. Without it there is no way to tell a
# detector that saw nothing from one that saw the car at 0.31 and discarded it,
# which is the difference between "move the camera" and "lower the threshold".
DEBUG = os.environ.get("DETECT_DEBUG", "").strip().lower() not in ("", "0", "false", "no")
# A floor for the debug log: below this the model is reporting noise in every
# frame and the log would say nothing.
DEBUG_FLOOR = float(os.environ.get("DETECT_DEBUG_FLOOR", "0.15"))

# What frames are decoded at before they reach the model, which letterboxes them
# to its own size. Large enough not to have to invent detail at 640, small
# enough to stay cheap to decode.
FRAME_SIZE = int(os.environ.get("DETECT_FRAME_SIZE", "640"))


def overlap(a, b) -> float:
    """How much two boxes share, as intersection over union."""
    if not a or not b:
        return 0.0
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    left, top = max(ax1, bx1), max(ay1, by1)
    right, bottom = min(ax2, bx2), min(ay2, by2)
    if right <= left or bottom <= top:
        return 0.0
    both = (right - left) * (bottom - top)
    either = ((ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - both)
    return both / either if either > 0 else 0.0


# How much a box has to overlap the last one reported before it counts as the
# same thing still sitting there. A car that parks is detected in every frame
# for as long as it stays; without this it would be reported for ever.
STILL_OVERLAP = float(os.environ.get("DETECT_STILL_OVERLAP", "0.6"))


class Tracker:
    """Turns a stream of per-frame results into events worth reporting.

    Three rules keep it quiet:

    * a class has to show up in several consecutive frames before it counts,
      which throws away the single-frame flickers a model produces;
    * once reported it goes quiet for a while, so one person walking past is
      one event rather than one per frame;
    * and something that has not moved since it was reported is not reported
      again. A parked car is in every frame it is parked in, and a log full of
      it is a log nobody reads.

    Counted per fine class rather than per coarse type: a car and a bicycle in
    the same driveway are two things happening, and reporting one because the
    other was already moving would be wrong.
    """

    def __init__(self, min_hits: int = 3, cooldown: float = 30.0,
                 still_overlap: float = STILL_OVERLAP):
        self.min_hits = max(1, min_hits)
        self.cooldown = cooldown
        self.still_overlap = still_overlap
        self._hits: dict[str, int] = {}
        self._last_sent: dict[str, float] = {}
        self._last_box: dict[str, tuple] = {}

    def update(self, present: dict,
               now: float | None = None) -> list[tuple[str, float, tuple]]:
        """present maps class -> (score, box). Returns what to report."""
        now = time.monotonic() if now is None else now
        events = []

        for name in set(self._hits) | set(present):
            if name not in present:
                # A gap resets the run, so hits have to be consecutive -- and
                # it forgets where the thing was, so the same car returning to
                # the same spot later is a new event rather than the old one.
                self._hits[name] = 0
                self._last_box.pop(name, None)
                continue

            score, box = present[name]
            self._hits[name] = self._hits.get(name, 0) + 1
            if self._hits[name] < self.min_hits:
                continue
            last = self._last_sent.get(name)
            if last is not None and now - last < self.cooldown:
                continue
            if overlap(box, self._last_box.get(name)) >= self.still_overlap:
                # Same place as last time: it has not gone anywhere. Keep the
                # box current so slow drift does not accumulate into a report.
                self._last_box[name] = box
                continue
            self._last_sent[name] = now
            self._last_box[name] = box
            events.append((name, score, box))
        return events


def frame_reader(source_url: str, transport: str, fps: float) -> subprocess.Popen:
    """ffmpeg decoding the stream into raw frames for the model."""
    args = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if source_url.startswith("rtsp"):
        args += ["-rtsp_transport", transport or "tcp"]
    args += [
        "-i", source_url,
        "-vf", f"fps={fps},scale={FRAME_SIZE}:{FRAME_SIZE}",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-",
    ]
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


class Detector(threading.Thread):
    """Watches a stream and calls back when something shows up."""

    FRAME_BYTES = FRAME_SIZE * FRAME_SIZE * 3

    def __init__(self, cfg, on_event, model=None, relay_url: str = "",
                 on_frame=None):
        super().__init__(name="detect", daemon=True)
        self.cfg = cfg
        self.on_event = on_event
        # Called for every analysed frame with whatever was above threshold,
        # where on_event fires only for what survived the tracker. Motion is a
        # state and needs the raw view: with only the tracked events to go on,
        # a person standing still would make it flap on and off.
        self.on_frame = on_frame
        self.model = model
        self.relay_url = relay_url
        self.tracker = Tracker(cfg.detect_min_hits, cfg.detect_cooldown)
        self.wanted = classes.expand(cfg.detect_types)
        self.error = ""
        self.frames = 0
        self._last_debug = 0.0
        self._stop = threading.Event()

    def source(self) -> str:
        """Prefer the sub stream: smaller frames, same objects.

        Through the relay when one is running. Reading the camera directly would
        be a second connection to it on top of the relay's, and a camera that
        allows only a few at once then drops one of them -- which shows up as a
        stream that will not start, nowhere near the detector that caused it.
        """
        if self.relay_url:
            return self.relay_url
        if self.cfg.source_url_sub:
            return self.cfg.source_url_sub
        return self.cfg.source_url

    def transport(self) -> str:
        """How to read whatever source() picked.

        The configured transport describes how to reach the *real* camera. When
        the detector is reading our own relay over loopback instead, that
        setting does not apply: TCP there costs nothing, never fragments and
        never drops, where UDP inherits whatever the socket buffers do under
        load.
        """
        if self.relay_url:
            return "tcp"
        return self.cfg.rtsp_transport

    def run(self):
        if self.model is None:
            try:
                self.model = Model(getattr(self.cfg, "detect_model", DEFAULT_MODEL))
            except ModelUnavailable as exc:
                self.error = str(exc)
                print(f"[detect] disabled: {exc}")
                return
            print(
                f"[detect] model {self.model.name} at {self.model.size}px "
                f"on {self.model.provider}"
            )

        while not self._stop.is_set():
            proc = frame_reader(self.source(), self.transport(), self.cfg.detect_fps)
            print(
                f"[detect] watching {self.source()} at {self.cfg.detect_fps} fps "
                f"for {', '.join(sorted(self.wanted)) or 'nothing'}"
            )
            try:
                self._consume(proc)
            except Exception as exc:  # noqa: BLE001 - restart rather than die
                self.error = str(exc)[:200]
                print(f"[detect] restarting after error: {exc}")
            finally:
                if proc.poll() is None:
                    proc.terminate()
            if self._stop.wait(5):
                return

    def _consume(self, proc):
        import numpy as np

        while not self._stop.is_set():
            raw = proc.stdout.read(self.FRAME_BYTES)
            if len(raw) < self.FRAME_BYTES:
                return  # stream ended; the outer loop reopens it
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                FRAME_SIZE, FRAME_SIZE, 3
            )
            self.frames += 1
            self.handle_frame(frame)

    def handle_frame(self, frame):
        best: dict[str, tuple] = {}
        seen: list[tuple[str, float]] = []
        # The model is told what is wanted so it does not run suppression over
        # classes nobody asked about; the debug log wants the rest, though.
        asked = None if DEBUG else self.wanted
        for name, score, box in self.model.infer(frame, asked):
            if DEBUG and score >= DEBUG_FLOOR:
                seen.append((name, score))
            if name not in self.wanted or score < self.cfg.detect_confidence:
                continue
            if score > best.get(name, (0.0, None))[0]:
                best[name] = (score, box)

        if seen:
            self._log_candidates(seen)

        if self.on_frame is not None:
            try:
                self.on_frame({name: score for name, (score, _) in best.items()})
            except Exception as exc:  # noqa: BLE001 - a bad sink must not stop us
                print(f"[detect] could not report presence: {exc}")

        for name, score, box in self.tracker.update(best):
            coarse = classes.coarse_of(name)
            print(f"[detect] {name} ({score:.2f})")
            try:
                self.on_event(name, score, coarse, box)
            except Exception as exc:  # noqa: BLE001 - a bad sink must not stop us
                print(f"[detect] could not report {name}: {exc}")

    def _log_candidates(self, seen):
        """One line a second at most, so the log stays readable."""
        now = time.monotonic()
        if now - self._last_debug < 1.0:
            return
        self._last_debug = now
        ranked = sorted(seen, key=lambda pair: -pair[1])[:4]
        wanted = ", ".join(
            f"{name} {score:.2f}" + ("" if name in self.wanted else " (not wanted)")
            for name, score in ranked
        )
        print(
            f"[detect] candidates: {wanted}"
            f" | reporting at >= {self.cfg.detect_confidence:.2f}"
            f" after {self.tracker.min_hits} frames in a row"
        )

    def stop(self):
        self._stop.set()
