"""Where detection stills live, and how their names carry their metadata.

Shared by both roles: the camera writes them, the controller lists, serves and
prunes them. Keeping the layout in one place means the two cannot disagree about
where a file is.

    <state>/events/<camera id>/<YYYY-MM-DD>/<epoch ms>-<type>-<score>.jpg

Everything about an event is in its path, so there is no index to keep in step
with the files -- delete a file and the event is gone, copy one in and it
appears. An index would be a second source of truth that has to be rewritten on
every prune, and a half-written one would be worse than no index at all.

The day directory is not decoration: a full store holds a few hundred thousand
stills, and listing one day at a time keeps that off the critical path.
"""

from __future__ import annotations

import os
import re
import time

DIR_NAME = "events"

# <epoch ms>-<type>-<score>[-<box>].jpg, for example
#   1759152000123-person-091-0120-0340-0560-0880.jpg
#
# The box is four thousandths-of-the-frame values: left, top, right, bottom. It
# is optional because stills written before there was one are still perfectly
# good stills, and a store that rejected them would be throwing away history to
# tidy up a filename.
_NAME = re.compile(
    r"^(\d{10,16})-([a-z]+)-(\d{1,3})"
    r"(?:-(\d{4})-(\d{4})-(\d{4})-(\d{4}))?\.jpg$"
)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def root(state_dir: str) -> str:
    return os.path.join(state_dir, DIR_NAME)


def camera_dir(state_dir: str, cam_id: str) -> str:
    return os.path.join(root(state_dir), cam_id)


def day_of(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(epoch_seconds))


def filename(epoch_seconds: float, object_type: str, score: float,
             box=None) -> str:
    """A name that is also the record. Sorts by time within a day."""
    millis = int(epoch_seconds * 1000)
    safe_type = re.sub(r"[^a-z]", "", (object_type or "unknown").lower()) or "unknown"
    graded = int(round(max(0.0, min(1.0, score)) * 100))
    stem = f"{millis}-{safe_type}-{graded:03d}"
    try:
        corners = [float(value) for value in (box or ())]
    except (TypeError, ValueError):
        # Whatever was handed in was not a box. A still without an outline is
        # worth far more than no still at all, so it is simply left off.
        corners = []
    if len(corners) == 4:
        # Thousandths, clamped: a box the model put slightly outside the frame
        # would otherwise not survive the round trip.
        parts = "-".join(
            f"{int(round(max(0.0, min(1.0, value)) * 1000)):04d}" for value in corners
        )
        stem = f"{stem}-{parts}"
    return f"{stem}.jpg"


def parse(name: str) -> dict | None:
    """The event a filename describes, or None if it is not one of ours."""
    match = _NAME.match(name)
    if not match:
        return None
    millis, object_type, score = match.group(1, 2, 3)
    corners = match.group(4, 5, 6, 7)
    event = {
        "name": name,
        "at": int(millis) / 1000.0,
        "type": object_type,
        "score": int(score) / 100.0,
    }
    if all(corners):
        event["box"] = [int(value) / 1000.0 for value in corners]
    return event


def is_day(name: str) -> bool:
    return bool(_DAY.match(name))


def safe_day(value: str) -> str:
    """A day string that cannot climb out of the store, or '' if it is not one."""
    value = (value or "").strip()
    return value if _DAY.match(value) else ""


def safe_name(value: str) -> str:
    """A filename that cannot climb out of the store, or '' if it is not ours."""
    value = (value or "").strip()
    return value if _NAME.match(value) else ""
