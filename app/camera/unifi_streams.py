"""Correct the stream descriptions unifi-cam-proxy reports to Protect.

The proxy answers Protect's ChangeVideoSettings with a fixed table, the same for
every camera it has ever run:

    video1  1920x1080  15 fps  max 2.8 Mbps  h264
    video2  1280x720   15 fps  max 1.2 Mbps  h264
    video3   640x360   15 fps  max 200 kbps  h264

None of it is measured. A camera sending 1080p at 10 fps is announced as 15, and
a channel that receives the full main stream is announced as 640x360 at 200 kbps
-- fourteen times under what arrives. Protect sizes its live view around those
numbers, so the low channels are where it shows.

What the streams really are is known in the camera process, which probes them.
It is handed over in UNIFI_STREAM_SPECS, and applied here to the proxy's own
answer rather than replacing it: the reply carries a great deal besides the
numbers, including the destinations that start and stop the streams.
"""

from __future__ import annotations

import json
import os

# Which key in the proxy's reply gets which measurement. Bitrates are in bits
# per second there and kbps here.
_FPS_KEYS = ("fps", "maxFps")


def load_specs(raw: str | None = None) -> dict:
    """The measurements, or {} when the camera process did not supply any."""
    raw = os.environ.get("UNIFI_STREAM_SPECS", "") if raw is None else raw
    if not raw:
        return {}
    try:
        specs = json.loads(raw)
    except ValueError:
        return {}
    return specs if isinstance(specs, dict) else {}


def _codec_name(codec: str) -> str:
    """ffprobe's name for the codec, as the proxy's reply spells it."""
    name = (codec or "").strip().lower()
    if name in ("hevc", "h265", "hvc1"):
        return "h265"
    if name in ("h264", "avc", "avc1"):
        return "h264"
    return ""


def apply(channel: dict, spec: dict) -> dict:
    """Overwrite one channel description in place with what was measured.

    Anything not measured is left alone. A probe that failed gives a width of 0,
    and a zero would be worse than the proxy's guess.
    """
    width, height = int(spec.get("width") or 0), int(spec.get("height") or 0)
    if width and height:
        channel["width"], channel["height"] = width, height

    fps = int(spec.get("fps") or 0)
    if fps:
        for key in _FPS_KEYS:
            channel[key] = fps
        valid = channel.get("validFpsValues")
        if isinstance(valid, list) and fps not in valid:
            # Protect reads this as what the camera can be set to. A rate that
            # is not in it is a rate it will not ask for.
            channel["validFpsValues"] = sorted({*valid, fps})

    kbps = int(spec.get("bitrate_kbps") or 0)
    if kbps:
        bits = kbps * 1000
        channel["bitRateCbrAvg"] = bits
        channel["bitRateVbrMax"] = bits
        channel["validBitrateRangeMax"] = max(bits, channel.get("validBitrateRangeMax", 0))
        if "currentVbrBitrate" in channel:
            channel["currentVbrBitrate"] = bits

    codec = _codec_name(spec.get("codec", ""))
    if codec:
        # The proxy writes h264 for every channel. A camera set to hand out
        # HEVC would otherwise be described as something it is not.
        channel["type"] = codec
    return channel


def correct(payload: dict, specs: dict) -> dict:
    """Apply every measurement to the proxy's reply. Returns the same payload."""
    video = (payload or {}).get("video")
    if not isinstance(video, dict) or not specs:
        return payload
    for channel_name, spec in specs.items():
        channel = video.get(channel_name)
        if isinstance(channel, dict) and isinstance(spec, dict):
            apply(channel, spec)
    return payload
