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
# How the encoder is asked to spend the bitrate.
#
#   vbr -- a ceiling, not a floor. A still scene costs almost nothing and a
#          busy one is allowed up to the figure asked for. Fewer bytes stored
#          and a better picture for the same average.
#   cbr -- the same rate whether anything is happening or not, padded if
#          necessary. Wasteful, and the right answer when the link has a fixed
#          budget or an NVR plans its disk from the advertised rate: a stream
#          that triples when a lorry goes past is the one that drops frames.
RATE_MODES = ("vbr", "cbr")

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


def scale_filter(width: int, height: int, hwaccel: str) -> str:
    """The scaler that matches the pixel format the decoder is producing.

    With hardware decoding the frames stay on the device, so a software scaler
    would have to pull them back through system memory and push them out again.
    Each accelerator has its own filter for staying put.
    """
    # Encoders reject odd dimensions, and rounding down loses at most a pixel.
    width, height = (width // 2) * 2, (height // 2) * 2
    if hwaccel == "vaapi":
        return f"scale_vaapi=w={width}:h={height}"
    if hwaccel == "qsv":
        return f"scale_qsv=w={width}:h={height}"
    if hwaccel == "nvenc":
        return f"scale_cuda={width}:{height}"
    return f"scale={width}:{height}"


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
    scale: tuple[int, int] | None = None,
    rate_mode: str = "vbr",
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

    if scale:
        args += ["-vf", scale_filter(scale[0], scale[1], hwaccel)]

    args += ["-c:v", encoder]

    # VAAPI takes its quality from the driver, not from a preset name.
    if hwaccel in ("none", "qsv"):
        args += ["-preset", preset]
    elif hwaccel == "nvenc":
        args += ["-preset", "p4", "-tune", "ll"]

    args += rate_args(encoder, hwaccel, bitrate_kbps, gop, rate_mode)

    args += ["-c:a", "copy"] if audio == "copy" else ["-an"]
    args += ["-f", "rtsp", "-rtsp_transport", "tcp", publish_url]
    return args


def rate_args(encoder: str, hwaccel: str, bitrate_kbps: int, gop: int,
              rate_mode: str = "vbr") -> list[str]:
    """How the encoder is told to spend its bitrate.

    Every encoder spells this differently and none of them infer it. Asking for
    constant and getting capped-variable is the kind of difference you only
    discover when a disk fills early or a link does not.
    """
    cbr = (rate_mode or "vbr").lower() == "cbr"
    rate = f"{int(bitrate_kbps)}k"
    args = ["-b:v", rate, "-maxrate", rate]
    if cbr:
        # A floor as well as a ceiling, and a buffer of exactly one second: a
        # larger one is what lets a "constant" rate wander.
        args += ["-minrate", rate, "-bufsize", rate]
    else:
        args += ["-bufsize", f"{int(bitrate_kbps) * 2}k"]
    args += ["-g", str(gop)]

    extra = f"keyint={gop}:scenecut=0"
    if encoder == "libx264":
        # nal-hrd=cbr makes x264 pad to rate rather than merely aim at it.
        args += ["-profile:v", "main",
                 "-x264-params", extra + (":nal-hrd=cbr:filler=1" if cbr else "")]
    elif encoder == "libx265":
        args += ["-x265-params",
                 extra + ":log-level=error" + (":strict-cbr=1" if cbr else "")]
    elif hwaccel == "nvenc":
        args += ["-rc", "cbr" if cbr else "vbr"]
    elif hwaccel == "vaapi":
        args += ["-rc_mode", "CBR" if cbr else "VBR"]
    # QSV has no flag for this: it infers the mode from the rates it was given,
    # and a ceiling equal to the target reads as constant. Asking for variable
    # on QSV therefore gets something close to constant. Saying so is better
    # than lowering the target behind the operator's back to prove a point.
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
