"""Camera configuration storage: a single JSON file guarded by a lock."""

from __future__ import annotations

import json
import os
import threading

from ..common import models


class CameraStore:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "cameras.json")
        self._lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)
        if not os.path.exists(self.path):
            self._write([])

    # ------------------------------------------------------------------- basics

    def _read(self) -> list[dict]:
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _write(self, cameras: list[dict]):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(cameras, fh, indent=2)
        os.replace(tmp, self.path)

    # -------------------------------------------------------------------- crud

    def list(self) -> list[dict]:
        with self._lock:
            return sorted(self._read(), key=lambda c: c.get("created_at", 0))

    def get(self, cam_id: str) -> dict | None:
        with self._lock:
            for cam in self._read():
                if cam["id"] == cam_id:
                    return cam
        return None

    def add(self, payload: dict) -> dict:
        cam = models.new_camera(payload)
        with self._lock:
            cameras = self._read()
            cameras.append(cam)
            self._write(cameras)
        return cam

    def update(self, cam_id: str, payload: dict) -> dict | None:
        with self._lock:
            cameras = self._read()
            for index, cam in enumerate(cameras):
                if cam["id"] == cam_id:
                    cam.update(models.sanitize(payload))
                    cameras[index] = cam
                    self._write(cameras)
                    return cam
        return None

    def delete(self, cam_id: str) -> bool:
        with self._lock:
            cameras = self._read()
            remaining = [c for c in cameras if c["id"] != cam_id]
            if len(remaining) == len(cameras):
                return False
            self._write(remaining)
        return True
