"""What the detector can tell apart, and what it reports as.

Two levels, because two different readers want different things. An NVR's event
filter understands person, vehicle and animal and nothing else -- those are the
ONVIF classes. A person looking at the timeline wants to know it was a truck
rather than a bicycle.

So every detection carries both: the fine class the model named, and the coarse
one it belongs to. The coarse type drives the events; the fine one is shown
beside the still and can be selected on.

The list is exactly what the model is trained on. Nothing is inferred from
something adjacent -- a parcel on a doorstep is not a suitcase, and calling it
one would be a guess dressed up as a detection.
"""

from __future__ import annotations

PERSON = "person"
VEHICLE = "vehicle"
ANIMAL = "animal"

COARSE = (PERSON, VEHICLE, ANIMAL)

# The 80 COCO classes, in the order the model emits them. Kept whole rather than
# trimmed: the index is the model's class id, so a gap would shift everything
# after it.
COCO_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)

# Which of those are worth reporting, and what each belongs to. Everything else
# the model knows -- a sofa, a toothbrush -- is ignored: it is not what a camera
# outside is for, and every extra class is another way to raise a false alarm.
#
# A van has no class of its own here because it has none in the model either. A
# delivery van comes back as a car or a truck depending on its shape, and
# inventing a third answer from the two would be guessing.
FINE = {
    "person": PERSON,
    "bicycle": VEHICLE,
    "car": VEHICLE,
    "motorcycle": VEHICLE,
    "bus": VEHICLE,
    "train": VEHICLE,
    "truck": VEHICLE,
    "boat": VEHICLE,
    "bird": ANIMAL,
    "cat": ANIMAL,
    "dog": ANIMAL,
    "horse": ANIMAL,
    "sheep": ANIMAL,
    "cow": ANIMAL,
    "bear": ANIMAL,
}

# What ONVIF calls each coarse type, for the ObjectType an NVR reads.
ONVIF_TYPE = {PERSON: "Human", VEHICLE: "Vehicle", ANIMAL: "Animal"}


def coarse_of(name: str) -> str:
    """The reportable type a fine class belongs to, or '' if it is not one."""
    return FINE.get((name or "").lower(), "")


def expand(selection: str) -> set[str]:
    """Turn a saved selection into the set of fine classes to report.

    A coarse name selects everything under it, so "person,vehicle" keeps
    working exactly as it did before there were fine classes -- which is what
    every camera configured so far has stored.
    """
    wanted: set[str] = set()
    for part in (selection or "").split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name in COARSE:
            wanted |= {fine for fine, group in FINE.items() if group == name}
        elif name in FINE:
            wanted.add(name)
    return wanted


def validate(selection: str) -> list[str]:
    """Names in a selection that mean nothing to the model."""
    unknown = []
    for part in (selection or "").split(","):
        name = part.strip().lower()
        if name and name not in COARSE and name not in FINE:
            unknown.append(name)
    return unknown
