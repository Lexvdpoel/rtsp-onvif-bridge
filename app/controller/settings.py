"""Settings that apply to the whole bridge rather than to one camera.

A single small JSON file. Unknown keys are dropped and out-of-range values are
clamped on the way in, so a hand-edited file cannot put the controller into a
state its own form could not produce.
"""

from __future__ import annotations

import json
import os
import threading

DEFAULTS = {
    # How much disk the detection stills may take, across every camera. Ten
    # gigabytes is roughly two hundred thousand stills, which is months for a
    # quiet camera and days for a busy one -- and either way the oldest go
    # first, so it never grows past this.
    "clip_budget_gb": 10.0,
}

LIMITS = {"clip_budget_gb": (0.1, 2000.0)}


class SettingsStore:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "settings.json")
        self._lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)

    def all(self) -> dict:
        with self._lock:
            stored = {}
            try:
                with open(self.path) as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    stored = loaded
            except (OSError, json.JSONDecodeError):
                pass
            return {**DEFAULTS, **self._clean(stored)}

    def update(self, payload: dict) -> dict:
        with self._lock:
            merged = {**self.all(), **self._clean(payload)}
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as fh:
                json.dump(merged, fh, indent=2)
            os.replace(tmp, self.path)
            return merged

    @staticmethod
    def _clean(payload: dict) -> dict:
        out = {}
        for key, value in (payload or {}).items():
            if key not in DEFAULTS:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            low, high = LIMITS[key]
            out[key] = min(high, max(low, number))
        return out
