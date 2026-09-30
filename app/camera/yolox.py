"""The detection model: YOLOX, run through ONNX Runtime.

Replaces SSD MobileNet v1, which was from 2017, worked on a 300x300 image and
was weak on exactly the case that matters outdoors -- something small and far
away. On the same two photographs the old model could say only "vehicle" and
"animal"; this one says "bus" at 0.94 and "dog" at 0.90.

Two sizes are carried. Accurate is YOLOX-S at 640 and is what to run unless the
host cannot afford it; fast is YOLOX-Tiny at 416, roughly a third of the work
and noticeably less sure of itself -- on the test photograph it called a dog a
cat. Which to use is a per-camera choice because the cost is per camera.

Everything the model needs doing to its input and output is here rather than in
a library: letterboxing, grid decoding and non-maximum suppression are twenty
lines of numpy, and a dependency for them would be a dependency to keep current.

YOLOX is Apache-2.0 (Megvii). That matters: the better-known YOLOv8 is AGPL-3.0,
which would make this project's own licence unusable.
"""

from __future__ import annotations

import os

from . import classes

MODELS = {
    # name -> (file, input size)
    "accurate": ("yolox_s.onnx", 640),
    "fast": ("yolox_tiny.onnx", 416),
}
DEFAULT_MODEL = "accurate"
MODEL_DIR = os.environ.get("DETECT_MODEL_DIR", "/opt/models")

# The three feature-map strides YOLOX predicts on. Every box comes from one of
# them, and decoding is undoing that.
STRIDES = (8, 16, 32)

# Below this a detection is not worth running suppression over. The configured
# confidence is applied after, on what survives.
FLOOR = 0.10


class ModelUnavailable(RuntimeError):
    pass


def model_path(name: str) -> str:
    filename, _ = MODELS.get(name, MODELS[DEFAULT_MODEL])
    return os.path.join(MODEL_DIR, filename)


def input_size(name: str) -> int:
    _, size = MODELS.get(name, MODELS[DEFAULT_MODEL])
    return size


def providers() -> list:
    """CUDA when this build has it and a card is present, otherwise the CPU.

    Asking for a provider that is not installed makes onnxruntime raise rather
    than fall back, so the list is built from what it says it has.
    """
    try:
        import onnxruntime
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise ModelUnavailable("onnxruntime is not installed") from exc

    available = onnxruntime.get_available_providers()
    wanted = []
    if os.environ.get("DETECT_GPU", "1") not in ("0", "false", "no"):
        for name in ("TensorrtExecutionProvider", "CUDAExecutionProvider"):
            if name in available:
                wanted.append(name)
    wanted.append("CPUExecutionProvider")
    return wanted


class Model:
    """One loaded network, behind a surface small enough to fake in tests."""

    def __init__(self, name: str = DEFAULT_MODEL, path: str = ""):
        try:
            import onnxruntime
        except ImportError as exc:  # pragma: no cover - depends on the image
            raise ModelUnavailable("onnxruntime is not installed") from exc

        self.name = name if name in MODELS else DEFAULT_MODEL
        self.size = input_size(self.name)
        path = path or model_path(self.name)
        if not os.path.exists(path):
            raise ModelUnavailable(f"model file missing: {path}")

        options = onnxruntime.SessionOptions()
        # One camera should not take every core; several run side by side.
        options.intra_op_num_threads = int(os.environ.get("DETECT_THREADS", "1"))
        options.inter_op_num_threads = 1
        chosen = providers()
        self.session = onnxruntime.InferenceSession(path, options, providers=chosen)
        self.provider = (self.session.get_providers() or ["?"])[0]
        self.input_name = self.session.get_inputs()[0].name
        self._grid = None

    # ------------------------------------------------------------------ pieces

    def grid(self):
        """Anchor centres and strides, built once and reused every frame."""
        import numpy as np

        if self._grid is None:
            centres, strides = [], []
            for stride in STRIDES:
                count = self.size // stride
                xs, ys = np.meshgrid(np.arange(count), np.arange(count))
                cell = np.stack((xs, ys), 2).reshape(1, -1, 2)
                centres.append(cell)
                strides.append(np.full((1, cell.shape[1], 1), stride))
            self._grid = (
                np.concatenate(centres, 1).astype("float32"),
                np.concatenate(strides, 1).astype("float32"),
            )
        return self._grid

    def prepare(self, frame):
        """A letterboxed BGR tensor, and the scale it was shrunk by.

        Letterboxed rather than stretched: YOLOX was trained that way, and a
        stretched frame moves every box slightly. The model wants BGR, and what
        arrives from ffmpeg is RGB.
        """
        import numpy as np

        height, width = frame.shape[:2]
        ratio = min(self.size / height, self.size / width)
        new_h, new_w = int(height * ratio), int(width * ratio)

        # Nearest-neighbour by indexing: no image library needed, and at these
        # sizes the difference against a proper resize is not measurable in the
        # detections.
        rows = (np.arange(new_h) / ratio).astype(np.int32).clip(0, height - 1)
        cols = (np.arange(new_w) / ratio).astype(np.int32).clip(0, width - 1)
        resized = frame[rows][:, cols]

        padded = np.full((self.size, self.size, 3), 114, dtype=np.uint8)
        padded[:new_h, :new_w] = resized[:, :, ::-1]  # RGB in, BGR out
        tensor = padded.transpose(2, 0, 1)[None].astype(np.float32)
        return np.ascontiguousarray(tensor), ratio

    def decode(self, raw):
        """Raw predictions to corner boxes and per-class scores."""
        import numpy as np

        centres, strides = self.grid()
        preds = raw[0].copy()
        xy = (preds[..., :2] + centres[0]) * strides[0]
        wh = np.exp(preds[..., 2:4]) * strides[0]
        boxes = np.stack([
            xy[:, 0] - wh[:, 0] / 2, xy[:, 1] - wh[:, 1] / 2,
            xy[:, 0] + wh[:, 0] / 2, xy[:, 1] + wh[:, 1] / 2,
        ], 1)
        scores = preds[:, 4:5] * preds[:, 5:]
        return boxes, scores

    @staticmethod
    def suppress(boxes, scores, threshold: float = 0.45):
        """Classic non-maximum suppression over one class."""
        import numpy as np

        x1, y1, x2, y2 = boxes.T
        areas = (x2 - x1) * (y2 - y1)
        order = scores.argsort()[::-1]
        keep = []
        while order.size:
            best = order[0]
            keep.append(best)
            if order.size == 1:
                break
            xx1 = np.maximum(x1[best], x1[order[1:]])
            yy1 = np.maximum(y1[best], y1[order[1:]])
            xx2 = np.minimum(x2[best], x2[order[1:]])
            yy2 = np.minimum(y2[best], y2[order[1:]])
            overlap = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
            union = areas[best] + areas[order[1:]] - overlap
            iou = np.where(union > 0, overlap / np.maximum(union, 1e-9), 0)
            order = order[1:][iou <= threshold]
        return keep

    # ------------------------------------------------------------------- public

    def infer(self, frame, wanted: set[str] | None = None):
        """frame: uint8 HxWx3 RGB. Returns [(fine class, score, box)].

        Boxes are normalised to the frame, so a caller does not have to know
        what size the model works at.
        """
        import numpy as np

        tensor, ratio = self.prepare(frame)
        raw = self.session.run(None, {self.input_name: tensor})[0]
        boxes, scores = self.decode(raw)
        boxes = boxes / ratio

        height, width = frame.shape[:2]
        results = []
        # Only the classes anyone asked for: suppression is the expensive part,
        # and running it over a toothbrush helps nobody.
        for index, name in enumerate(classes.COCO_NAMES):
            if name not in classes.FINE:
                continue
            if wanted is not None and name not in wanted:
                continue
            column = scores[:, index]
            above = column > FLOOR
            if not above.any():
                continue
            candidates, confidences = boxes[above], column[above]
            for keep in self.suppress(candidates, confidences):
                x1, y1, x2, y2 = candidates[keep]
                results.append((
                    name,
                    float(confidences[keep]),
                    (float(x1 / width), float(y1 / height),
                     float(x2 / width), float(y2 / height)),
                ))
        return results
