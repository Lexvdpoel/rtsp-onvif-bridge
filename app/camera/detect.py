"""Object detection on the camera's own stream.

Dumb cameras send pixels and nothing else, so the detection has to happen here.
It runs on the sub stream: a few frames a second at low resolution is enough to
say "a person is there", and it keeps the cost per camera small enough to run
several at once on a CPU.

The model is SSD MobileNet v1 from the ONNX model zoo, trained on COCO. It does
its own non-maximum suppression, so what comes out is already a short list of
boxes rather than a field of overlapping candidates.

UniFi Protect groups detections into a handful of types. COCO's classes are
mapped onto those; COCO has no class for a parcel, so package detection is not
possible with this model and is deliberately absent rather than faked from
"suitcase".
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

MODEL_PATH = os.environ.get("DETECT_MODEL", "/opt/models/ssd_mobilenet_v1_10.onnx")

# The model wants a square uint8 image; TensorFlow's pipeline trained it by
# stretching to 300x300, so stretch rather than letterbox.
INPUT_SIZE = 300

PERSON = "person"
VEHICLE = "vehicle"
ANIMAL = "animal"

# COCO class ids as the TensorFlow label map numbers them, which is what this
# model emits.
COCO_TO_TYPE = {
    1: PERSON,
    2: VEHICLE,   # bicycle
    3: VEHICLE,   # car
    4: VEHICLE,   # motorcycle
    6: VEHICLE,   # bus
    7: VEHICLE,   # train
    8: VEHICLE,   # truck
    16: ANIMAL,   # bird
    17: ANIMAL,   # cat
    18: ANIMAL,   # dog
    19: ANIMAL,   # horse
    20: ANIMAL,   # sheep
    21: ANIMAL,   # cow
    22: ANIMAL,   # elephant
    23: ANIMAL,   # bear
    24: ANIMAL,   # zebra
    25: ANIMAL,   # giraffe
}

ALL_TYPES = (PERSON, VEHICLE, ANIMAL)


class ModelUnavailable(RuntimeError):
    pass


class Model:
    """The ONNX session, kept behind a small surface so it can be faked in tests."""

    def __init__(self, path: str = MODEL_PATH):
        try:
            import onnxruntime  # imported here so the rest works without it
        except ImportError as exc:  # pragma: no cover - depends on the image
            raise ModelUnavailable("onnxruntime is not installed") from exc
        if not os.path.exists(path):
            raise ModelUnavailable(f"model file missing: {path}")

        options = onnxruntime.SessionOptions()
        # One camera should not take every core; several run side by side.
        options.intra_op_num_threads = int(os.environ.get("DETECT_THREADS", "1"))
        options.inter_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            path, options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

    def infer(self, frame):
        """frame: uint8 HxWx3. Returns [(type, score, box)] above nothing yet."""
        import numpy as np

        batch = np.expand_dims(frame, axis=0)
        boxes, classes, scores, count = self.session.run(
            None, {self.input_name: batch}
        )
        results = []
        for index in range(int(count[0])):
            object_type = COCO_TO_TYPE.get(int(classes[0][index]))
            if object_type is None:
                continue
            # boxes are normalised ymin, xmin, ymax, xmax
            ymin, xmin, ymax, xmax = (float(v) for v in boxes[0][index])
            results.append(
                (object_type, float(scores[0][index]), (xmin, ymin, xmax, ymax))
            )
        return results


class Tracker:
    """Turns a stream of per-frame results into events worth reporting.

    Two rules keep it quiet: a type has to show up in several consecutive frames
    before it counts, which throws away the single-frame flickers a small model
    produces, and once reported it goes quiet for a while so one person walking
    past does not generate an event per frame.
    """

    def __init__(self, min_hits: int = 3, cooldown: float = 30.0):
        self.min_hits = max(1, min_hits)
        self.cooldown = cooldown
        self._hits: dict[str, int] = {}
        self._last_sent: dict[str, float] = {}

    def update(self, present: dict[str, float], now: float | None = None) -> list[tuple[str, float]]:
        """present maps type -> best score this frame. Returns types to report."""
        now = time.monotonic() if now is None else now
        events = []

        for object_type in ALL_TYPES:
            if object_type in present:
                self._hits[object_type] = self._hits.get(object_type, 0) + 1
                if self._hits[object_type] < self.min_hits:
                    continue
                last = self._last_sent.get(object_type)
                if last is not None and now - last < self.cooldown:
                    continue
                self._last_sent[object_type] = now
                events.append((object_type, present[object_type]))
            else:
                # A gap resets the run, so hits have to be consecutive.
                self._hits[object_type] = 0
        return events


def frame_reader(source_url: str, transport: str, fps: float) -> subprocess.Popen:
    """ffmpeg decoding the stream into raw frames at the size the model wants."""
    args = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if source_url.startswith("rtsp"):
        args += ["-rtsp_transport", transport or "tcp"]
    args += [
        "-i", source_url,
        "-vf", f"fps={fps},scale={INPUT_SIZE}:{INPUT_SIZE}",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-",
    ]
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


class UnifiSink:
    """Hands a detection to the proxy process over loopback."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.host = host
        self.port = port
        self._sock = None

    def _connect(self):
        import socket

        self._sock = socket.create_connection((self.host, self.port), timeout=5)

    def __call__(self, object_type: str, score: float):
        import socket

        payload = f"{object_type} {score:.3f}\n".encode()
        for attempt in (1, 2):
            try:
                if self._sock is None:
                    self._connect()
                self._sock.sendall(payload)
                return
            except OSError:
                # The proxy restarts independently, so one dropped connection is
                # expected; reconnect once before giving up on this event.
                if self._sock is not None:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                self._sock = None
                if attempt == 2:
                    raise


class Detector(threading.Thread):
    """Watches a stream and calls back when something shows up."""

    FRAME_BYTES = INPUT_SIZE * INPUT_SIZE * 3

    def __init__(self, cfg, on_event, model=None):
        super().__init__(name="detect", daemon=True)
        self.cfg = cfg
        self.on_event = on_event
        self.model = model
        self.tracker = Tracker(cfg.detect_min_hits, cfg.detect_cooldown)
        self.wanted = {t for t in ALL_TYPES if t in cfg.detect_types}
        self.error = ""
        self.frames = 0
        self._stop = threading.Event()

    def source(self) -> str:
        """Prefer the sub stream: smaller frames, same objects."""
        if self.cfg.source_url_sub:
            return self.cfg.source_url_sub
        return self.cfg.source_url

    def run(self):
        if self.model is None:
            try:
                self.model = Model()
            except ModelUnavailable as exc:
                self.error = str(exc)
                print(f"[detect] disabled: {exc}")
                return

        while not self._stop.is_set():
            proc = frame_reader(self.source(), self.cfg.rtsp_transport, self.cfg.detect_fps)
            print(f"[detect] watching {self.source()} at {self.cfg.detect_fps} fps")
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
                INPUT_SIZE, INPUT_SIZE, 3
            )
            self.frames += 1
            self.handle_frame(frame)

    def handle_frame(self, frame):
        best: dict[str, float] = {}
        for object_type, score, _box in self.model.infer(frame):
            if object_type not in self.wanted or score < self.cfg.detect_confidence:
                continue
            if score > best.get(object_type, 0.0):
                best[object_type] = score

        for object_type, score in self.tracker.update(best):
            print(f"[detect] {object_type} ({score:.2f})")
            try:
                self.on_event(object_type, score)
            except Exception as exc:  # noqa: BLE001 - a bad sink must not stop us
                print(f"[detect] could not report {object_type}: {exc}")

    def stop(self):
        self._stop.set()
