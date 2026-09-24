"""Read the real properties of the incoming stream with ffprobe.

The ONVIF encoder configuration is metadata: an NVR trusts what the camera says
about resolution, frame rate and bitrate. Rather than making the operator type
those in and hope they match, probe the source once at startup and advertise
what is actually there.
"""

from __future__ import annotations

import json
import subprocess

PROBE_TIMEOUT = 25


def _fraction(value: str) -> float:
    """ffprobe reports frame rates as 'num/den'."""
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return round(float(num) / den_f, 3) if den_f else 0.0
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


def probe(source_url: str, transport: str = "tcp") -> dict:
    """Return what could be read about the first video stream.

    Always returns a dict; `ok` says whether anything usable came back, and
    `error` carries the reason when it did not.
    """
    cmd = ["ffprobe", "-v", "error"]
    if source_url.startswith("rtsp"):
        cmd += ["-rtsp_transport", transport or "tcp"]
    cmd += [
        "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,bit_rate,profile,pix_fmt"
        ":format=bit_rate",
        "-of", "json",
        source_url,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffprobe timed out on the source"}
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe is not installed"}

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return {"ok": False, "error": (detail[-1] if detail else "ffprobe failed")[:200]}

    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "could not parse the ffprobe output"}

    streams = data.get("streams") or []
    if not streams:
        return {"ok": False, "error": "no video stream found"}
    stream = streams[0]

    fps = _fraction(stream.get("avg_frame_rate", "")) or _fraction(
        stream.get("r_frame_rate", "")
    )

    # RTSP sources usually leave bit_rate empty; the measured rate fills that gap.
    bitrate_kbps = 0
    for candidate in (stream.get("bit_rate"), (data.get("format") or {}).get("bit_rate")):
        try:
            if candidate and int(candidate) > 0:
                bitrate_kbps = round(int(candidate) / 1000)
                break
        except (TypeError, ValueError):
            continue

    return {
        "ok": True,
        "error": "",
        "codec": (stream.get("codec_name") or "").lower(),
        "profile": stream.get("profile") or "",
        "pix_fmt": stream.get("pix_fmt") or "",
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": fps,
        "bitrate_kbps": bitrate_kbps,
    }


def onvif_encoding(codec: str) -> str:
    """Map an ffprobe codec name onto an ONVIF Encoding value."""
    return {
        "h264": "H264",
        "hevc": "H265",
        "h265": "H265",
        "mjpeg": "JPEG",
        "mpeg4": "MPEG4",
    }.get((codec or "").lower(), "H264")
