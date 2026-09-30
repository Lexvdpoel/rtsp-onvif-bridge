"""Writes live frames into the shared volume, but only while someone watches.

The controller cannot read this camera over the network -- the cameras are on a
macvlan network that a container on Docker's bridge cannot route to -- so the
frames go through the volume both already share.

Demand-driven on purpose. An encoder per camera running whether or not anyone is
looking would be a permanent cost for an occasional view, and on a host already
relaying three streams and running three detectors that is the difference
between comfortable and not. The controller renews a request while a browser is
attached; a few seconds after it stops, so does this.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from ..common import mjpeg

# How often to look for a change in what is being asked for. A viewer waits at
# most this long for the first frame, which is well inside the time a browser
# takes to lay out the grid.
POLL_SECONDS = 1.0


class LiveWriter(threading.Thread):
    def __init__(self, cam_id: str, state_dir: str, main_url: str, sub_url: str = ""):
        super().__init__(name="live", daemon=True)
        self.cam_id = cam_id
        self.state_dir = state_dir
        self.main_url = main_url
        self.sub_url = sub_url
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._spec: dict | None = None
        self.frames = 0
        self.error = ""

    def source_for(self, quality: str) -> str:
        if quality == "low" and self.sub_url:
            return self.sub_url
        return self.main_url

    def run(self):
        while not self._stop.is_set():
            spec = mjpeg.wanted(self.state_dir, self.cam_id)
            if spec is None:
                self._halt()
                self._stop.wait(POLL_SECONDS)
                continue
            if spec != self._spec:
                # A different quality or size means a different encoder.
                self._halt()
                self._spec = spec
                print(
                    f"[live] someone is watching: {spec['quality']} stream at "
                    f"{spec['fps']} fps, {spec['width']}px"
                )
            try:
                self._pump(spec)
            except Exception as exc:  # noqa: BLE001 - never fatal to the camera
                self.error = str(exc)[:200]
                print(f"[live] encoder stopped: {exc}")
                self._halt()
                self._stop.wait(POLL_SECONDS)
        self._halt()

    def _pump(self, spec: dict):
        """Run one encoder, writing frames until nobody is watching any more."""
        source = self.source_for(spec["quality"])
        if not source:
            self._stop.wait(POLL_SECONDS)
            return
        self._proc = subprocess.Popen(
            mjpeg.command(source, spec["fps"], spec["width"]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.makedirs(mjpeg.live_dir(self.state_dir), exist_ok=True)
        path = mjpeg.frame_path(self.state_dir, self.cam_id)
        tmp = f"{path}.tmp"
        produced = 0
        try:
            for jpeg in mjpeg.split_frames(self._proc.stdout):
                if self._stop.is_set():
                    return
                try:
                    with open(tmp, "wb") as fh:
                        fh.write(jpeg)
                    # Moved into place rather than written in place: the
                    # controller has no lock on this file and must never read
                    # half a picture.
                    os.replace(tmp, path)
                except OSError as exc:
                    # A full disk, or a reader holding the file open on a
                    # filesystem that minds. One lost frame is not a reason to
                    # tear down an encoder that is otherwise working.
                    self.error = str(exc)[:200]
                    continue
                produced += 1
                self.frames += 1
                # Checked here rather than on a timer: this loop only turns when
                # frames arrive, which is exactly when the question matters.
                if produced % max(1, spec["fps"]) == 0:
                    if mjpeg.wanted(self.state_dir, self.cam_id) != spec:
                        return
        finally:
            if not produced:
                self._report(source)
            self._halt()

    def _report(self, source: str):
        detail = ""
        if self._proc and self._proc.stderr:
            try:
                detail = (self._proc.stderr.read() or b"").decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
        first = detail.strip().splitlines()[0] if detail.strip() else "no output"
        self.error = first[:200]
        print(f"[live] no frames from {source}: {first}")

    def _halt(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        for stream in (proc.stdout, proc.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
        self._halt()
        # Wait for the loop to notice before clearing up, or it writes one more
        # frame after the file has been removed and the picture comes back from
        # the dead.
        if threading.current_thread() is not self and self.is_alive():
            self.join(timeout=3)
        # The frame left behind is a picture from whenever this stopped, and a
        # tile showing it would be showing the past as the present.
        try:
            os.remove(mjpeg.frame_path(self.state_dir, self.cam_id))
        except OSError:
            pass
        # Every viewer's request too: they belong to a camera that is going
        # away, and a leftover one would ask the next start to encode for
        # nobody.
        prefix = os.path.basename(mjpeg.demand_path(self.state_dir, self.cam_id))
        directory = mjpeg.live_dir(self.state_dir)
        try:
            for name in os.listdir(directory):
                if name.startswith(prefix):
                    try:
                        os.remove(os.path.join(directory, name))
                    except OSError:
                        pass
        except OSError:
            pass
