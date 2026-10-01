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


class Motion:
    """How much of the picture changed since the frame before it.

    A model asked the same question of the same still picture does not always
    give the same answer. It will occasionally find a person in a hedge or a car
    in a pattern of shadows, hold that opinion for a frame or two, and drop it
    -- and on a scene where nothing has moved, that is the only thing it can be.

    So a detection is weighed against what changed. The comparison is made on a
    quarter-scale greyscale copy: a shape large enough to be worth reporting is
    still several pixels there, and it keeps this to about a millisecond on a
    frame the detector spends a hundred times longer on.
    """

    SCALE = 4

    def __init__(self, noise: float = 4.0):
        # Mean absolute difference, in levels out of 255, below which a region
        # counts as unchanged. Sensor noise at night sits a long way under this.
        self.noise = noise
        self._previous = None
        self._diff = None

    def update(self, frame):
        import numpy as np

        small = frame[:: self.SCALE, :: self.SCALE].astype(np.float32).mean(axis=2)
        previous, self._previous = self._previous, small
        if previous is None or previous.shape != small.shape:
            # A stream that comes back at a different size has no frame to be
            # compared with. Saying so costs one sighting; subtracting two
            # different shapes would take the detector down.
            self._diff = None
            return
        self._diff = np.abs(small - previous)

    def moved(self, box) -> bool:
        """Whether the region inside box changed enough to be movement.

        Without a previous frame the answer is no, not yes. "I cannot tell" and
        "it moved" are different things, and treating the first as the second
        would let the first frame after every reconnect through unchecked --
        which is exactly when a model is most likely to be guessing.
        """
        if self._diff is None or not box:
            return False
        height, width = self._diff.shape
        x1 = max(0, min(width - 1, int(box[0] * width)))
        y1 = max(0, min(height - 1, int(box[1] * height)))
        x2 = max(x1 + 1, min(width, int(round(box[2] * width))))
        y2 = max(y1 + 1, min(height, int(round(box[3] * height))))
        region = self._diff[y1:y2, x1:x2]
        return region.size > 0 and float(region.mean()) >= self.noise


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

# How much a region has to change, in levels out of 255, before it counts as
# having moved. Sensor noise on a dark scene sits well under this; raise it on a
# camera whose picture crawls, lower it for something that moves very slowly.
MOTION_NOISE = float(os.environ.get("DETECT_MOTION_NOISE", "4"))


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
                 still_overlap: float = STILL_OVERLAP, require_motion: bool = True):
        self.min_hits = max(1, min_hits)
        self.cooldown = cooldown
        self.still_overlap = still_overlap
        self.require_motion = require_motion
        self._hits: dict[str, int] = {}
        self._last_sent: dict[str, float] = {}
        self._last_box: dict[str, tuple] = {}
        self._stirred: dict[str, bool] = {}

    def update(self, present: dict, moved: set | None = None,
               now: float | None = None):
        """present maps class -> (score, box); moved names those that stirred.

        Returns (events, confirmed): what to report, and which classes are
        present on a run that has seen movement. The second is what anyone
        downstream should treat as really being there.
        """
        now = time.monotonic() if now is None else now
        moved = moved or set()
        events = []
        confirmed = set()

        for name in set(self._hits) | set(present):
            if name not in present:
                # A gap resets the run, so hits have to be consecutive -- and
                # it forgets where the thing was, so the same car returning to
                # the same spot later is a new event rather than the old one.
                self._hits[name] = 0
                self._last_box.pop(name, None)
                self._stirred.pop(name, None)
                continue

            score, box = present[name]
            self._hits[name] = self._hits.get(name, 0) + 1
            # Movement anywhere in the run counts, not movement in this frame.
            # Someone who walks into view and then stands still moved when they
            # arrived, and is no less there for having stopped.
            if name in moved:
                self._stirred[name] = True
            if self.require_motion and not self._stirred.get(name):
                continue
            confirmed.add(name)
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
        return events, confirmed


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
        self.tracker = Tracker(
            cfg.detect_min_hits, cfg.detect_cooldown,
            require_motion=getattr(cfg, "detect_motion", True),
        )
        self.motion = Motion(MOTION_NOISE)
        self.wanted = classes.expand(cfg.detect_types)
        self.error = ""
        self.frames = 0
        self._last_debug = 0.0
        self._last_still = 0.0
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

        # Which of them stirred since the last frame. Worked out here, once,
        # rather than per candidate: the difference image is the same for all.
        self.motion.update(frame)
        moved = {name for name, (_, box) in best.items() if self.motion.moved(box)}
        if DEBUG and best and not moved:
            self._log_still(best)

        events, confirmed = self.tracker.update(best, moved)

        if self.on_frame is not None:
            try:
                # Only what is confirmed: a shape the model found in a hedge
                # that has not moved is not something to tell an NVR about.
                self.on_frame({name: best[name][0] for name in confirmed})
            except Exception as exc:  # noqa: BLE001 - a bad sink must not stop us
                print(f"[detect] could not report presence: {exc}")

        for name, score, box in events:
            coarse = classes.coarse_of(name)
            print(f"[detect] {name} ({score:.2f})")
            try:
                # The frame goes with it: a still has to be the moment the box
                # describes, not one fetched a second later.
                self.on_event(name, score, coarse, box, frame)
            except Exception as exc:  # noqa: BLE001 - a bad sink must not stop us
                print(f"[detect] could not report {name}: {exc}")

    def _log_still(self, best):
        """Say when something was found but nothing moved, under DETECT_DEBUG.

        This is the case that used to produce a phantom car at three in the
        morning, so it is worth being able to watch it being thrown away.
        """
        now = time.monotonic()
        if now - self._last_still < 2.0:
            return
        self._last_still = now
        named = ", ".join(f"{name} {score:.2f}" for name, (score, _) in best.items())
        print(f"[detect] ignored (nothing moved there): {named}")

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
