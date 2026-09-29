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

from . import unifi_logfilter, unifi_streams

DETECT_PORT = int(os.environ.get("DETECT_BRIDGE_PORT", "8099"))

# How long a motion event stays open after the last detection. It has to be
# closed: trigger_motion_start does nothing while an event is already open, so
# an event left hanging swallows every detection after the first one.
MOTION_HOLD = float(os.environ.get("DETECT_MOTION_HOLD", "8"))

# What this camera can actually recognise. Only these three: the model behind the
# detector has no class for a parcel, and nothing here does face recognition,
# licence plates or line crossing, so claiming them would put filters in
# Protect's timeline that never match anything.
SMART_DETECT_TYPES = ("person", "vehicle", "animal")


def advertised_detections() -> list[str]:
    """The smart-detect capability to declare, or nothing when detection is off.

    unifi-cam-proxy declares mic, aec, videoMode and motionDetect, and no
    smartDetect at all. Protect gates its smart detection on that capability, so
    a camera that never claims it can send EventSmartDetect all day and see
    nothing appear in the timeline. Declared here, from the same configuration
    the detector runs on, so the claim and the behaviour cannot drift apart.
    """
    if os.environ.get("DETECT", "0").strip() not in ("1", "true", "yes", "on"):
        return []
    wanted = {
        part.strip().lower()
        for part in (os.environ.get("DETECT_TYPES") or "").split(",")
        if part.strip()
    }
    return [name for name in SMART_DETECT_TYPES if name in wanted]


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

        async def process_video_settings(self, msg):
            # The proxy's own answer first: it carries the destinations that
            # start and stop the streams, and dropping any of that to correct a
            # few numbers would trade a cosmetic fault for a real one.
            response = await super().process_video_settings(msg)
            specs = unifi_streams.load_specs()
            if specs and isinstance(response, dict):
                unifi_streams.correct(response.get("payload"), specs)
            return response

        async def get_feature_flags(self) -> dict:
            flags = await super().get_feature_flags()
            detections = advertised_detections()
            if detections:
                flags = dict(flags, smartDetect=list(detections))
                self.logger.info(
                    "Declaring smart detection for: %s", ", ".join(detections)
                )
            return flags

        async def run(self) -> None:
            self._motion_deadline = None
            self._detect_server = await asyncio.start_server(
                self._handle_detection, "127.0.0.1", DETECT_PORT
            )
            self.logger.info("Listening for detections on 127.0.0.1:%s", DETECT_PORT)
            self._motion_closer = asyncio.ensure_future(self._close_idle_motion())
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
                    # Extend first: a detection arriving while the closer is
                    # about to fire must keep the event open, not race it shut.
                    self._motion_deadline = (
                        asyncio.get_event_loop().time() + MOTION_HOLD
                    )
                    try:
                        await self.trigger_motion_start(_object_type(name))
                    except Exception as exc:  # noqa: BLE001
                        self.logger.warning("Could not report %s: %s", name, exc)
            finally:
                writer.close()

        async def _close_idle_motion(self) -> None:
            """End a motion event once the detections stop arriving.

            Nothing else does this. The proxy only closes an event on shutdown or
            through its optional HTTP API, and trigger_motion_start is a no-op
            while one is open, so without this the first detection would be the
            only one Protect ever hears about.
            """
            while True:
                await asyncio.sleep(1)
                deadline = self._motion_deadline
                if deadline is None:
                    continue
                if asyncio.get_event_loop().time() < deadline:
                    continue
                self._motion_deadline = None
                try:
                    await self.trigger_motion_stop()
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("Could not end the motion event: %s", exc)

    return DetectingRTSPCam


def main() -> int:
    from unifi import main as unifi_main

    camera_class = build_camera_class()
    unifi_main.CAMS["rtsp"] = camera_class
    # The filter goes on before the proxy builds its loggers; getLogger
    # hands back the same object either way, so the names are enough.
    unifi_logfilter.install(camera_class.__name__, "Core")
    return unifi_main.main()


if __name__ == "__main__":
    sys.exit(main())
