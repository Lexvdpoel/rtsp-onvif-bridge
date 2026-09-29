"""Reads the detection stills the cameras write, and keeps them within budget.

The cameras only ever add. Something has to take away, or the volume fills and
the first thing anyone notices is that the array is full -- which is a far worse
way to find out than a chart that stops going up.

Pruning is oldest-first across every camera, not per camera. A budget divided
per camera would leave a busy driveway discarding this morning while a quiet
back garden keeps last month, which is the wrong trade for anyone who has to go
looking.
"""

from __future__ import annotations

import os
import shutil
import threading
import time

from ..common import clips

GIGABYTE = 1024 ** 3


class ClipStore:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.root = clips.root(state_dir)
        self._lock = threading.Lock()
        self.last_prune: dict = {}

    # ------------------------------------------------------------------ reading

    def days(self, cam_id: str) -> list[dict]:
        """Every day this camera has stills for, newest first, with counts."""
        directory = clips.camera_dir(self.state_dir, cam_id)
        out = []
        try:
            names = os.listdir(directory)
        except OSError:
            return out
        for name in names:
            if not clips.is_day(name):
                continue
            try:
                files = os.listdir(os.path.join(directory, name))
            except OSError:
                continue
            count = sum(1 for f in files if clips.parse(f))
            if count:
                out.append({"day": name, "count": count})
        return sorted(out, key=lambda d: d["day"], reverse=True)

    def events(self, cam_id: str, day: str, limit: int = 2000) -> list[dict]:
        """Every event on one day, oldest first, as the timeline reads them."""
        day = clips.safe_day(day)
        if not day:
            return []
        directory = os.path.join(clips.camera_dir(self.state_dir, cam_id), day)
        out = []
        try:
            names = os.listdir(directory)
        except OSError:
            return out
        for name in names:
            event = clips.parse(name)
            if event is None:
                continue
            try:
                event["bytes"] = os.path.getsize(os.path.join(directory, name))
            except OSError:
                continue
            out.append(event)
        out.sort(key=lambda e: e["at"])
        return out[-limit:]

    def path_of(self, cam_id: str, day: str, name: str) -> str:
        """The file for one event, or '' when the request does not name one.

        Both parts are matched against their patterns rather than merely
        cleaned: a name that is not one of ours cannot be turned into one that
        is, so there is no path to escape through.
        """
        day, name = clips.safe_day(day), clips.safe_name(name)
        if not day or not name or not cam_id.isalnum():
            return ""
        path = os.path.join(clips.camera_dir(self.state_dir, cam_id), day, name)
        return path if os.path.isfile(path) else ""

    def forget(self, cam_id: str):
        """Drop everything for one camera, for when the camera itself is gone."""
        if not cam_id.isalnum():
            return
        shutil.rmtree(clips.camera_dir(self.state_dir, cam_id), ignore_errors=True)

    # ------------------------------------------------------------------ pruning

    def _walk(self) -> list[tuple[float, int, str]]:
        """(timestamp, size, path) for every still, across all cameras."""
        found = []
        try:
            cameras = os.listdir(self.root)
        except OSError:
            return found
        for cam_id in cameras:
            camera_path = os.path.join(self.root, cam_id)
            try:
                days = os.listdir(camera_path)
            except OSError:
                continue
            for day in days:
                if not clips.is_day(day):
                    continue
                day_path = os.path.join(camera_path, day)
                try:
                    names = os.listdir(day_path)
                except OSError:
                    continue
                for name in names:
                    event = clips.parse(name)
                    if event is None:
                        continue
                    path = os.path.join(day_path, name)
                    try:
                        found.append((event["at"], os.path.getsize(path), path))
                    except OSError:
                        continue
        return found

    def usage(self) -> dict:
        files = self._walk()
        return {
            "bytes": sum(size for _, size, _ in files),
            "files": len(files),
            "oldest": min((at for at, _, _ in files), default=0.0),
        }

    def prune(self, budget_bytes: int) -> dict:
        """Delete oldest-first until the store fits. Returns what it did."""
        with self._lock:
            files = self._walk()
            total = sum(size for _, size, _ in files)
            removed = freed = 0
            if total > budget_bytes:
                files.sort(key=lambda item: item[0])
                for _, size, path in files:
                    if total <= budget_bytes:
                        break
                    try:
                        os.remove(path)
                    except OSError:
                        continue
                    total -= size
                    freed += size
                    removed += 1
                self._sweep_empty_days()
            self.last_prune = {
                "at": time.time(),
                "bytes": total,
                "files": len(files) - removed,
                "removed": removed,
                "freed": freed,
                "budget": budget_bytes,
            }
            if removed:
                print(
                    f"[clips] removed {removed} of the oldest stills "
                    f"({freed / GIGABYTE:.2f} GB) to stay under "
                    f"{budget_bytes / GIGABYTE:.1f} GB"
                )
            return self.last_prune

    def _sweep_empty_days(self):
        """A day directory with nothing left in it is only clutter."""
        try:
            cameras = os.listdir(self.root)
        except OSError:
            return
        for cam_id in cameras:
            camera_path = os.path.join(self.root, cam_id)
            try:
                days = os.listdir(camera_path)
            except OSError:
                continue
            for day in days:
                if not clips.is_day(day):
                    continue
                day_path = os.path.join(camera_path, day)
                try:
                    if not os.listdir(day_path):
                        os.rmdir(day_path)
                except OSError:
                    continue
