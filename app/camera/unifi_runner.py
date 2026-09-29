"""Runs unifi-cam-proxy with a camera class that can report detections.

unifi-cam-proxy picks its backend from a registry keyed by subcommand name. We
replace the entry for "rtsp" with a subclass that also listens on loopback for
detections found by the detector in the main process, and reports them to
Protect through the proxy's own trigger_motion_start.

Keeping the proxy in its own process means a crash restarts just the proxy, and
the detector does not have to live inside its event loop.
"""

from __future__ import annotations

import asyncio
import os
import sys

DETECT_PORT = int(os.environ.get("DETECT_BRIDGE_PORT", "8099"))


def _object_type(name: str):
    """Map our detection type onto the proxy's enum.

    The enum ships with person and vehicle only. Protect itself knows about
    animals, so for that one a stand-in with the same .value is passed; if a
    Protect version rejects it, only animal events are lost.
    """
    from unifi.cams.base import SmartDetectObjectType

    for member in SmartDetectObjectType:
        if member.value == name:
            return member

    class _Extra:
        value = name

        def __repr__(self):
            return f"<SmartDetectObjectType.{name.upper()}>"

    return _Extra()


def build_camera_class():
    from unifi.cams.rtsp import RTSPCam

    class DetectingRTSPCam(RTSPCam):
        """RTSP backend that also accepts detections over loopback."""

        async def run(self) -> None:
            self._detect_server = await asyncio.start_server(
                self._handle_detection, "127.0.0.1", DETECT_PORT
            )
            self.logger.info("Listening for detections on 127.0.0.1:%s", DETECT_PORT)
            await super().run()

        async def _handle_detection(self, reader, writer):
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    parts = line.decode("utf-8", "replace").split()
                    if not parts:
                        continue
                    name = parts[0].strip().lower()
                    self.logger.info("Detection reported: %s", name)
                    try:
                        await self.trigger_motion_start(_object_type(name))
                    except Exception as exc:  # noqa: BLE001
                        self.logger.warning("Could not report %s: %s", name, exc)
            finally:
                writer.close()

    return DetectingRTSPCam


def main() -> int:
    from unifi import main as unifi_main

    unifi_main.CAMS["rtsp"] = build_camera_class()
    return unifi_main.main()


if __name__ == "__main__":
    sys.exit(main())
