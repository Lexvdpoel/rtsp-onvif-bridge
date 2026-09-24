"""Measure real throughput per camera from the RTSP relay.

MediaMTX counts the bytes it pulls from the source and the bytes it hands to
readers. Sampling those counters gives the actual incoming and outgoing bitrate,
and the spread of the incoming samples says whether the source encodes at a
constant or a variable rate.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

API_URL = "http://127.0.0.1:9997/v3/paths/list"
SAMPLE_SECONDS = 2.0
# Two minutes of history: enough spread for the rate-mode call, small enough to
# follow a scene change within a minute.
WINDOW = 60
# Below this coefficient of variation the source is holding a constant rate.
CBR_THRESHOLD = 0.08
MIN_SAMPLES_FOR_MODE = 15


class PathStats:
    """Rolling throughput for one relay path."""

    def __init__(self):
        self.in_bps = 0.0
        self.out_bps = 0.0
        self.bytes_in = 0
        self.bytes_out = 0
        self.readers = 0
        self.ready = False
        self.history: list[float] = []
        self._last: tuple[float, int, int] | None = None

    def update(self, now: float, bytes_in: int, bytes_out: int, readers: int, ready: bool):
        self.readers = readers
        self.ready = ready

        previous = self._last
        self._last = (now, bytes_in, bytes_out)
        self.bytes_in = bytes_in
        self.bytes_out = bytes_out
        if previous is None:
            return

        elapsed = now - previous[0]
        if elapsed <= 0:
            return
        # A relay restart resets the counters; treat a decrease as a fresh start.
        if bytes_in < previous[1] or bytes_out < previous[2]:
            self.history.clear()
            return

        self.in_bps = (bytes_in - previous[1]) * 8 / elapsed
        self.out_bps = (bytes_out - previous[2]) * 8 / elapsed

        if self.ready and self.in_bps > 0:
            self.history.append(self.in_bps)
            del self.history[:-WINDOW]

    def rate_mode(self) -> tuple[str, float]:
        """Classify the source's rate control from the measured spread.

        RTSP carries no rate-control flag, so this is inferred, not reported.
        """
        samples = self.history
        if len(samples) < MIN_SAMPLES_FOR_MODE:
            return "", 0.0
        mean = sum(samples) / len(samples)
        if mean <= 0:
            return "", 0.0
        variance = sum((s - mean) ** 2 for s in samples) / len(samples)
        cv = (variance ** 0.5) / mean
        return ("CBR" if cv < CBR_THRESHOLD else "VBR"), round(cv, 3)

    def snapshot(self) -> dict:
        mode, cv = self.rate_mode()
        return {
            "in_bps": round(self.in_bps),
            "out_bps": round(self.out_bps),
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "readers": self.readers,
            "ready": self.ready,
            "rate_mode": mode,
            "rate_cv": cv,
            "measured_kbps": round(sum(self.history) / len(self.history) / 1000)
            if self.history
            else 0,
        }


class StatsCollector(threading.Thread):
    """Polls the relay's API and keeps per-path throughput up to date."""

    def __init__(self, api_url: str = API_URL):
        super().__init__(name="relay-stats", daemon=True)
        self.api_url = api_url
        self.paths: dict[str, PathStats] = {}
        self.available = False
        self.error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(SAMPLE_SECONDS):
            try:
                payload = self._fetch()
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                with self._lock:
                    self.available = False
                    self.error = str(exc)[:200]
                continue

            now = time.monotonic()
            with self._lock:
                self.available = True
                self.error = ""
                for item in payload.get("items", []):
                    name = item.get("name")
                    if not name:
                        continue
                    stats = self.paths.setdefault(name, PathStats())
                    stats.update(
                        now,
                        int(item.get("bytesReceived") or 0),
                        int(item.get("bytesSent") or 0),
                        len(item.get("readers") or []),
                        bool(item.get("ready")),
                    )

    def _fetch(self) -> dict:
        request = urllib.request.Request(self.api_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=4) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def snapshot(self) -> dict:
        """Per-path stats plus a total, shaped for the state file."""
        with self._lock:
            paths = {name: stats.snapshot() for name, stats in self.paths.items()}
            available = self.available
            error = self.error

        return {
            "available": available,
            "error": error,
            "paths": paths,
            "in_bps": sum(p["in_bps"] for p in paths.values()),
            "out_bps": sum(p["out_bps"] for p in paths.values()),
            "readers": sum(p["readers"] for p in paths.values()),
            "ready": any(p["ready"] for p in paths.values()),
            "rate_mode": paths.get("main", {}).get("rate_mode", ""),
            "rate_cv": paths.get("main", {}).get("rate_cv", 0.0),
            "measured_kbps": paths.get("main", {}).get("measured_kbps", 0),
        }

    def stop(self):
        self._stop.set()
