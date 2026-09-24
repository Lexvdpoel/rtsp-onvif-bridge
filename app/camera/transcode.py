"""Build the ffmpeg command that re-encodes a source into the wanted codec.

Transcoding is the expensive path, so it is only taken when the source codec
differs from what the camera is configured to hand out. A source that already
matches is relayed untouched, at no CPU cost.

When a transcode does run, ffmpeg publishes into the local relay rather than the
relay pulling from the source, so the stream an NVR reads still comes from the
virtual camera's own address.
"""

from __future__ import annotations

import shlex

# What the operator can ask for, mapped onto the ffmpeg codec family.
OUTPUT_CODECS = ("copy", "h264", "h265")
HWACCELS = ("none", "vaapi", "qsv", "nvenc")

# Source codec names (as ffprobe reports them) per output family.
_EQUIVALENT = {
    "h264": {"h264", "avc", "avc1"},
    "h265": {"hevc", "h265", "hvc1"},
}

_ENCODERS = {
    ("h264", "none"): "libx264",
    ("h265", "none"): "libx265",
    ("h264", "vaapi"): "h264_vaapi",
    ("h265", "vaapi"): "hevc_vaapi",
    ("h264", "qsv"): "h264_qsv",
    ("h265", "qsv"): "hevc_qsv",
    ("h264", "nvenc"): "h264_nvenc",
    ("h265", "nvenc"): "hevc_nvenc",
}

VAAPI_DEVICE = "/dev/dri/renderD128"


def needs_transcode(source_codec: str, output_codec: str) -> bool:
    """True when the source has to be re-encoded to satisfy the request.

    An unknown source codec (the probe failed) counts as a mismatch: the
    operator asked for a specific codec, and delivering it matters more than
    saving the CPU we might not have needed to spend.
    """
    if output_codec not in ("h264", "h265"):
        return False
    return (source_codec or "").lower() not in _EQUIVALENT[output_codec]


def encoder_name(output_codec: str, hwaccel: str) -> str:
    return _ENCODERS.get((output_codec, hwaccel), _ENCODERS[(output_codec, "none")])


def build_args(
    source_url: str,
    publish_url: str,
    output_codec: str,
    hwaccel: str = "none",
    bitrate_kbps: int = 4096,
    preset: str = "veryfast",
    transport: str = "tcp",
    audio: str = "copy",
    gop: int = 30,
) -> list[str]:
    """Full ffmpeg argument list for one transcoded path."""
    encoder = encoder_name(output_codec, hwaccel)
    args = ["ffmpeg", "-nostdin", "-loglevel", "warning"]

    # Hardware decode has to be set up before the input is opened.
    if hwaccel == "vaapi":
        args += [
            "-hwaccel", "vaapi",
            "-hwaccel_device", VAAPI_DEVICE,
            "-hwaccel_output_format", "vaapi",
        ]
    elif hwaccel == "qsv":
        args += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    elif hwaccel == "nvenc":
        args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]

    if source_url.startswith("rtsp"):
        args += ["-rtsp_transport", transport or "tcp"]
    args += ["-i", source_url]

    args += ["-c:v", encoder]

    # VAAPI takes its quality from the driver, not from a preset name.
    if hwaccel in ("none", "qsv"):
        args += ["-preset", preset]
    elif hwaccel == "nvenc":
        args += ["-preset", "p4", "-tune", "ll"]

    rate = f"{int(bitrate_kbps)}k"
    args += [
        "-b:v", rate,
        "-maxrate", rate,
        "-bufsize", f"{int(bitrate_kbps) * 2}k",
        "-g", str(gop),
    ]

    if encoder == "libx264":
        args += ["-profile:v", "main", "-x264-params", f"keyint={gop}:scenecut=0"]
    elif encoder == "libx265":
        args += ["-x265-params", f"keyint={gop}:scenecut=0:log-level=error"]

    args += ["-c:a", "copy"] if audio == "copy" else ["-an"]
    args += ["-f", "rtsp", "-rtsp_transport", "tcp", publish_url]
    return args


def script(args: list[str]) -> str:
    """Wrap the command in a shell script.

    MediaMTX splits a runOnDemand command on spaces and expands `$NAME`, either
    of which would mangle a source URL containing a password. Putting the
    command in a script keeps the URL exactly as typed.
    """
    return "#!/bin/sh\nexec " + " ".join(shlex.quote(arg) for arg in args) + "\n"


def describe(source_codec: str, output_codec: str, hwaccel: str) -> str:
    """One line for the UI and the logs."""
    if output_codec == "copy":
        return f"{(source_codec or 'unknown').upper()} (relayed, no re-encode)"
    if not needs_transcode(source_codec, output_codec):
        return f"{output_codec.upper()} (source already matches, no re-encode)"
    where = "software" if hwaccel == "none" else hwaccel
    return f"{(source_codec or 'unknown').upper()} to {output_codec.upper()} ({where})"
