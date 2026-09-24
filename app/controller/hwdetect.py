"""Detect the host's usable hardware encoders, and pick one automatically.

The controller cannot see the host's GPU from inside its own container, so the
probe runs in a throwaway container started with the device attached. That also
means the answer comes from the same ffmpeg build that will do the real
encoding, rather than from a guess about what the host supports.

The result is cached, because it only changes when the hardware or the image
does.
"""

from __future__ import annotations

import json
import os
import threading
import time

from docker.errors import APIError, ImageNotFound, NotFound
from docker.types import DeviceRequest

CACHE_FILE = "hwaccel.json"
PROBE_TIMEOUT = 120

# Preference order when resolving "auto". VA-API first: it is the broadest path
# on the integrated graphics most hosts have, and unlike NVENC it has no limit
# on how many streams may encode at once. NVENC is last for that reason, not
# because it is slower - consumer cards cap concurrent encode sessions, which
# matters once several cameras convert at the same time.
PREFERENCE = ("vaapi", "qsv", "nvenc")


class HardwareDetector:
    def __init__(self, client, image: str, data_dir: str):
        self.client = client
        self.image = image
        self.path = os.path.join(data_dir, CACHE_FILE)
        self._lock = threading.Lock()

    # -------------------------------------------------------------------- cache

    def cached(self) -> dict:
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _store(self, report: dict):
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(report, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[hwdetect] could not cache the result: {exc}")

    def ensure(self) -> dict:
        """Cached result, probing once if nothing has been detected yet."""
        cached = self.cached()
        if cached.get("detected_at"):
            return cached
        return self.detect()

    # -------------------------------------------------------------------- probe

    def detect(self) -> dict:
        """Run the probe containers and cache what comes back."""
        with self._lock:
            report = {
                "detected_at": time.time(),
                "image": self.image,
                "available": {},
                "details": {},
                "errors": {},
            }

            # /dev/dri covers both Intel and AMD render nodes.
            self._collect(
                report, ["vaapi", "qsv"],
                {"devices": ["/dev/dri:/dev/dri:rwm"]},
                "No /dev/dri on the host, so no Intel or AMD render node.",
            )
            self._collect(
                report, ["nvenc"],
                {"device_requests": [DeviceRequest(count=-1, capabilities=[["gpu"]])]},
                "No NVIDIA GPU reachable; the NVIDIA container runtime may be missing.",
            )
            # Software always works; listing it keeps the UI honest about the choice.
            self._collect(report, ["none"], {}, "")

            report["recommended"] = {
                codec: self.best_for(codec, report) for codec in ("h264", "h265")
            }
            self._store(report)
            return report

    def _collect(self, report: dict, families: list[str], kwargs: dict, hint: str):
        try:
            output = self._run_probe(families, kwargs)
        except Exception as exc:  # noqa: BLE001 - every failure is just "not available"
            for family in families:
                report["available"][family] = {"h264": False, "h265": False}
                report["errors"][family] = hint or str(exc)[:200]
            return

        for family in families:
            results = (output.get("results") or {}).get(family, {})
            report["available"][family] = {
                codec: bool(results.get(codec, {}).get("ok")) for codec in ("h264", "h265")
            }
            failures = {
                codec: results.get(codec, {}).get("error", "")
                for codec in ("h264", "h265")
                if not results.get(codec, {}).get("ok")
            }
            if failures:
                report["errors"][family] = "; ".join(
                    f"{codec}: {error}" for codec, error in failures.items() if error
                )
        report["details"] = {
            "render_nodes": output.get("render_nodes", report["details"].get("render_nodes", [])),
            "driver": output.get("driver") or report["details"].get("driver", ""),
        }

    def _run_probe(self, families: list[str], kwargs: dict) -> dict:
        try:
            self.client.images.get(self.image)
        except ImageNotFound as exc:
            raise RuntimeError(f"Image '{self.image}' is not built yet.") from exc

        container = self.client.containers.create(
            image=self.image,
            command=["python", "-m", "app.camera.hwprobe", *families],
            entrypoint=[""],  # bypass the role entrypoint; this is a one-off probe
            network_mode="none",
            labels={"rtsp-onvif-bridge.probe": "true"},
            **kwargs,
        )
        try:
            container.start()
            container.wait(timeout=PROBE_TIMEOUT)
            raw = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
        finally:
            try:
                container.remove(force=True)
            except (NotFound, APIError):
                pass

        for line in reversed(raw.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        raise RuntimeError("the probe produced no result")

    # ----------------------------------------------------------------- choosing

    def best_for(self, output_codec: str, report: dict | None = None) -> str:
        """The accelerator to use for a codec, or 'none' when there is none."""
        if output_codec not in ("h264", "h265"):
            return "none"
        available = (report if report is not None else self.cached()).get("available", {})
        for family in PREFERENCE:
            if available.get(family, {}).get(output_codec):
                return family
        return "none"

    def resolve(self, cam: dict) -> str:
        """Turn a camera's stored setting into a concrete accelerator."""
        requested = cam.get("hwaccel", "auto")
        if requested != "auto":
            return requested
        return self.best_for(cam.get("output_codec", "copy"))


def summarize(report: dict) -> str:
    """One line for the UI."""
    if not report.get("detected_at"):
        return "not detected yet"
    working = [
        family for family in PREFERENCE
        if any(report.get("available", {}).get(family, {}).values())
    ]
    if not working:
        return "software only"
    names = {"vaapi": "VA-API", "qsv": "Quick Sync", "nvenc": "NVENC"}
    return ", ".join(names[family] for family in working)
