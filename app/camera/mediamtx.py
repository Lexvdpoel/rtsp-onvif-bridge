"""Per-camera RTSP relay.

Running a relay inside the camera container means the RTSP URL advertised over
ONVIF points at the virtual camera's own IP, so a client such as UniFi Protect
only ever talks to one address per camera. The relay pulls from the upstream
source on demand, so an idle virtual camera puts no load on the real one.
"""

from __future__ import annotations

import os
import subprocess

CONFIG_PATH = "/tmp/mediamtx.yml"


def _path_block(name: str, source: str, transport: str) -> str:
    return (
        f"  {name}:\n"
        f"    source: {source}\n"
        f"    sourceOnDemand: yes\n"
        f"    sourceOnDemandStartTimeout: 15s\n"
        f"    sourceOnDemandCloseAfter: 20s\n"
        f"    rtspTransport: {transport}\n"
    )


def write_config(paths: dict[str, str], rtsp_port: int, transport: str = "tcp") -> str:
    """paths maps the published path name -> upstream source URL."""
    blocks = "".join(_path_block(name, src, transport) for name, src in paths.items())
    config = (
        "logLevel: info\n"
        "logDestinations: [stdout]\n"
        "readTimeout: 15s\n"
        "writeTimeout: 15s\n"
        "api: no\n"
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
