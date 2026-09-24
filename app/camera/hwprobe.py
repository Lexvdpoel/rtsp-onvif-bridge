"""Find out which hardware encoders actually work here.

Being listed by `ffmpeg -encoders` only means the encoder was compiled in, not
that this machine can run it: an older Intel iGPU lists `hevc_vaapi` but can
only decode H.265, and a render node may be present while the driver is not.

So every candidate is settled by encoding a handful of generated frames. If that
returns cleanly, the encoder works; there is nothing left to assume.

Runs inside a throwaway container that the controller starts with the right
device attached, and prints its findings as JSON on stdout.
"""

from __future__ import annotations

import json
import subprocess
import sys

VAAPI_DEVICE = "/dev/dri/renderD128"
TEST_TIMEOUT = 25

# Five frames of a generated pattern: enough to force the encoder to initialise
# and produce output, small enough to finish in well under a second.
_SOURCE = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=5", "-frames:v", "5"]


def _run(args: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(args, capture_output=True, timeout=TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except FileNotFoundError:
        return False, "ffmpeg not installed"
    if proc.returncode == 0:
        return True, ""
    stderr = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    return False, (stderr[-1] if stderr else f"exit {proc.returncode}")[:200]


def _encode_test(accel: str, codec: str) -> tuple[bool, str]:
    """Try a real encode with one accelerator/codec combination."""
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]

    if accel == "vaapi":
        encoder = "h264_vaapi" if codec == "h264" else "hevc_vaapi"
        args = (base + ["-vaapi_device", VAAPI_DEVICE] + _SOURCE
                + ["-vf", "format=nv12,hwupload", "-c:v", encoder, "-f", "null", "-"])
    elif accel == "qsv":
        encoder = "h264_qsv" if codec == "h264" else "hevc_qsv"
        args = (base + ["-init_hw_device", f"qsv=hw:{VAAPI_DEVICE}",
                        "-filter_hw_device", "hw"] + _SOURCE
                + ["-vf", "format=nv12,hwupload=extra_hw_frames=16",
                   "-c:v", encoder, "-f", "null", "-"])
    elif accel == "nvenc":
        encoder = "h264_nvenc" if codec == "h264" else "hevc_nvenc"
        args = base + _SOURCE + ["-c:v", encoder, "-f", "null", "-"]
    else:
        encoder = "libx264" if codec == "h264" else "libx265"
        args = base + _SOURCE + ["-c:v", encoder, "-preset", "ultrafast",
                                 "-f", "null", "-"]
    ok, error = _run(args)
    return ok, error


def listed_encoders() -> list[str]:
    ok, _ = _run(["ffmpeg", "-hide_banner", "-encoders"])
    if not ok:
        return []
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                          capture_output=True, timeout=TEST_TIMEOUT)
    text = proc.stdout.decode("utf-8", "replace")
    wanted = ("h264_vaapi", "hevc_vaapi", "h264_qsv", "hevc_qsv",
              "h264_nvenc", "hevc_nvenc", "libx264", "libx265")
    return [name for name in wanted if f" {name} " in text]


def render_nodes() -> list[str]:
    import glob
    return sorted(glob.glob("/dev/dri/render*"))


def vainfo() -> str:
    ok, _ = _run(["vainfo", "--display", "drm", "--device", VAAPI_DEVICE])
    if not ok:
        return ""
    proc = subprocess.run(["vainfo", "--display", "drm", "--device", VAAPI_DEVICE],
                          capture_output=True, timeout=TEST_TIMEOUT)
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if "Driver version" in line:
            return line.split(":", 1)[-1].strip()[:120]
    return ""


def probe(families: list[str]) -> dict:
    listed = listed_encoders()
    report: dict = {
        "listed_encoders": listed,
        "render_nodes": render_nodes(),
        "driver": vainfo() if {"vaapi", "qsv"} & set(families) else "",
        "results": {},
    }

    for accel in families:
        report["results"][accel] = {}
        for codec in ("h264", "h265"):
            encoder = {
                ("vaapi", "h264"): "h264_vaapi", ("vaapi", "h265"): "hevc_vaapi",
                ("qsv", "h264"): "h264_qsv", ("qsv", "h265"): "hevc_qsv",
                ("nvenc", "h264"): "h264_nvenc", ("nvenc", "h265"): "hevc_nvenc",
                ("none", "h264"): "libx264", ("none", "h265"): "libx265",
            }[(accel, codec)]
            if encoder not in listed:
                report["results"][accel][codec] = {
                    "ok": False, "error": "encoder not built into ffmpeg"
                }
                continue
            ok, error = _encode_test(accel, codec)
            report["results"][accel][codec] = {"ok": ok, "error": error}
    return report


def main() -> int:
    families = [a for a in sys.argv[1:] if a in ("vaapi", "qsv", "nvenc", "none")]
    if not families:
        families = ["none"]
    print(json.dumps(probe(families)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
