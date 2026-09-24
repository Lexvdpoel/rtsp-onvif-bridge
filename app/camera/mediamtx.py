"""Per-camera RTSP relay.

Running a relay inside the camera container means the RTSP URL advertised over
ONVIF points at the virtual camera's own IP, so a client such as UniFi Protect
only ever talks to one address per camera.

A path works one of two ways:

* **pull** - MediaMTX opens the source itself and passes the stream through
  untouched. No re-encoding, so no CPU cost.
* **publish** - ffmpeg re-encodes the source and pushes the result in. Used when
  the source codec is not the one the camera is configured to hand out.

Both are on demand: nothing is pulled or encoded while no one is watching.
"""

from __future__ import annotations

import os
import subprocess

CONFIG_PATH = "/tmp/mediamtx.yml"
SCRIPT_DIR = "/tmp"


def _pull_block(name: str, source: str, transport: str) -> str:
    return (
        f"  {name}:\n"
        f"    source: {source}\n"
        f"    sourceOnDemand: yes\n"
        f"    sourceOnDemandStartTimeout: 15s\n"
        f"    sourceOnDemandCloseAfter: 20s\n"
        f"    rtspTransport: {transport}\n"
    )


def _publish_block(name: str, script_path: str) -> str:
    return (
        f"  {name}:\n"
        f"    runOnDemand: /bin/sh {script_path}\n"
        f"    runOnDemandRestart: yes\n"
        f"    runOnDemandStartTimeout: 30s\n"
        f"    runOnDemandCloseAfter: 20s\n"
    )


def write_transcode_script(name: str, args: list[str], script_builder) -> str:
    path = os.path.join(SCRIPT_DIR, f"transcode-{name}.sh")
    with open(path, "w") as fh:
        fh.write(script_builder(args))
    os.chmod(path, 0o755)
    return path


def write_config(paths: dict[str, dict], rtsp_port: int, transport: str = "tcp") -> str:
    """`paths` maps a path name onto either {'source': url} or {'script': path}."""
    blocks = ""
    for name, spec in paths.items():
        if spec.get("script"):
            blocks += _publish_block(name, spec["script"])
        else:
            blocks += _pull_block(name, spec["source"], transport)

    config = (
        "logLevel: info\n"
        "logDestinations: [stdout]\n"
        "readTimeout: 15s\n"
        "writeTimeout: 15s\n"
        # Bound to loopback: it is only read by this container's stats collector.
        "api: yes\n"
        "apiAddress: 127.0.0.1:9997\n"
        "metrics: no\n"
        "pprof: no\n"
        "playback: no\n"
        "rtmp: no\n"
        "hls: no\n"
        "webrtc: no\n"
        "srt: no\n"
        "rtsp: yes\n"
        f"rtspAddress: :{rtsp_port}\n"
        "rtpAddress: :8000\n"
        "rtcpAddress: :8001\n"
        "paths:\n" + blocks
    )
    with open(CONFIG_PATH, "w") as fh:
        fh.write(config)
    return CONFIG_PATH


def start(config_path: str = CONFIG_PATH) -> subprocess.Popen | None:
    if not os.path.exists("/usr/local/bin/mediamtx"):
        print("[relay] mediamtx binary missing; falling back to direct source URLs")
        return None
    print("[relay] starting mediamtx")
    return subprocess.Popen(["/usr/local/bin/mediamtx", config_path])
