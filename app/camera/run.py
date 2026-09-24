"""Entrypoint for a single virtual ONVIF camera container.

Boot order:
  1. take a DHCP lease on the macvlan interface (own MAC -> own IP)
  2. start the RTSP relay so the stream lives on this camera's IP
  3. serve ONVIF over HTTP and answer WS-Discovery probes
  4. keep a small status file the web UI reads
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass

from . import mediamtx, net, probe as probe_mod
from .onvif_server import serve as serve_onvif
from .stats import StatsCollector
from .wsdiscovery import DiscoveryResponder

HEARTBEAT_SECONDS = 10


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0") in ("1", "true", "True", "yes")


@dataclass
class Config:
    id: str
    name: str
    uuid: str
    hostname: str
    serial: str
    manufacturer: str
    model: str
    firmware: str
    location: str
    source_url: str
    source_url_sub: str
    onvif_port: int
    rtsp_port: int
    username: str
    password: str
    require_auth: bool
    proxy: bool
    rtsp_transport: str
    snapshot_enabled: bool
    autodetect: bool
    width: int
    height: int
    fps: int
    bitrate: int
    width_sub: int
    height_sub: int
    fps_sub: int
    bitrate_sub: int
    # Replaced by the probed codec when autodetect is on.
    encoding: str = "H264"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            id=_env("CAM_ID", "unknown"),
            name=_env("CAM_NAME", "Camera"),
            uuid=_env("CAM_UUID", "00000000-0000-0000-0000-000000000000"),
            hostname=_env("CAM_HOSTNAME", "camera"),
            serial=_env("CAM_SERIAL", "000000000000"),
            manufacturer=_env("CAM_MANUFACTURER", "RTSP-ONVIF-Bridge"),
            model=_env("CAM_MODEL", "VirtualCam"),
            firmware=_env("CAM_FIRMWARE", "1.0.0"),
            location=_env("CAM_LOCATION", "any"),
            source_url=_env("SOURCE_URL"),
            source_url_sub=_env("SOURCE_URL_SUB"),
            onvif_port=_env_int("ONVIF_PORT", 80),
            rtsp_port=_env_int("RTSP_PORT", 554),
            username=_env("ONVIF_USER", "admin"),
            password=_env("ONVIF_PASS", "admin"),
            require_auth=_env_bool("REQUIRE_AUTH", True),
            proxy=_env_bool("PROXY", True),
            rtsp_transport=_env("RTSP_TRANSPORT", "tcp"),
            snapshot_enabled=_env_bool("SNAPSHOT", True),
            autodetect=_env_bool("AUTODETECT", True),
            width=_env_int("VIDEO_WIDTH", 1920),
            height=_env_int("VIDEO_HEIGHT", 1080),
            fps=_env_int("VIDEO_FPS", 15),
            bitrate=_env_int("VIDEO_BITRATE", 4096),
            width_sub=_env_int("VIDEO_WIDTH_SUB", 640),
            height_sub=_env_int("VIDEO_HEIGHT_SUB", 360),
            fps_sub=_env_int("VIDEO_FPS_SUB", 15),
            bitrate_sub=_env_int("VIDEO_BITRATE_SUB", 512),
        )


class State:
    """Live network state, shared with the ONVIF and discovery threads."""

    def __init__(self):
        self.ip = ""
        self.mac = ""
        self.prefix = 24
        self.gateway = ""
        self.status = "starting"
        self.message = ""
        self.detected: dict = {}
        self.stats: dict = {}

    def refresh_from_interface(self):
        self.ip = net.read_ip()
        self.mac = net.read_mac()
        self.prefix = net.read_prefix()
        self.gateway = net.read_gateway()


class StateFile:
    """Small JSON file in the shared volume; the controller reads it for the UI."""

    def __init__(self, cam_id: str, directory: str):
        self.path = os.path.join(directory, f"{cam_id}.json")
        os.makedirs(directory, exist_ok=True)

    def write(self, payload: dict):
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[state] could not write {self.path}: {exc}")


def _status_payload(cfg: Config, state: State) -> dict:
    return {
        "id": cfg.id,
        "name": cfg.name,
        "status": state.status,
        "message": state.message,
        "ip": state.ip,
        "mac": state.mac,
        "prefix": state.prefix,
        "gateway": state.gateway,
        "hostname": cfg.hostname,
        "onvif_url": f"http://{state.ip}:{cfg.onvif_port}/onvif/device_service"
        if state.ip
        else "",
        "rtsp_url": f"rtsp://{state.ip}:{cfg.rtsp_port}/main" if state.ip else "",
        "snapshot_url": f"http://{state.ip}:{cfg.onvif_port}/snapshot" if state.ip else "",
        "detected": state.detected,
        "stats": state.stats,
        "advertised": {
            "encoding": cfg.encoding,
            "width": cfg.width,
            "height": cfg.height,
            "fps": cfg.fps,
            "bitrate": cfg.bitrate,
            "autodetect": cfg.autodetect,
        },
        "updated_at": time.time(),
    }


def _probed_bitrate(state: State) -> int:
    """Bitrate ffprobe reported for the main stream, 0 when it reported none."""
    return (state.detected or {}).get("main", {}).get("bitrate_kbps", 0)


def _apply_probe(cfg: Config, state: State, sub: bool = False):
    """Probe a source and, when autodetect is on, advertise what was found."""
    url = cfg.source_url_sub if sub else cfg.source_url
    if not url:
        return
    result = probe_mod.probe(url, cfg.rtsp_transport)
    key = "sub" if sub else "main"
    state.detected = dict(state.detected or {})
    state.detected[key] = result

    if not result.get("ok"):
        print(f"[probe] {key}: {result.get('error')}")
        return
    print(
        f"[probe] {key}: {result['codec']} {result['width']}x{result['height']} "
        f"@ {result['fps']}fps"
    )
    if not cfg.autodetect:
        return

    if sub:
        if result["width"]:
            cfg.width_sub, cfg.height_sub = result["width"], result["height"]
        if result["fps"]:
            cfg.fps_sub = max(1, round(result["fps"]))
        if result["bitrate_kbps"]:
            cfg.bitrate_sub = result["bitrate_kbps"]
        return

    if result["width"]:
        cfg.width, cfg.height = result["width"], result["height"]
    if result["fps"]:
        cfg.fps = max(1, round(result["fps"]))
    if result["bitrate_kbps"]:
        cfg.bitrate = result["bitrate_kbps"]
    cfg.encoding = probe_mod.onvif_encoding(result["codec"])


def main() -> int:
    cfg = Config.from_env()
    state = State()
    state_file = StateFile(cfg.id, _env("STATE_DIR", "/state"))

    if not cfg.source_url:
        state.status = "error"
        state.message = "No source URL configured"
        state_file.write(_status_payload(cfg, state))
        print("[camera] SOURCE_URL is empty; nothing to serve", file=sys.stderr)
        return 1

    print(f"[camera] starting '{cfg.name}' ({cfg.id})")
    state.mac = net.read_mac()
    state_file.write(_status_payload(cfg, state))

    # 1. DHCP -------------------------------------------------------------
    dhcp_proc = None
    if _env_bool("USE_DHCP", True):
        lease_file = os.path.join("/tmp", "lease.env")
        dhcp_proc = net.start_dhcp(cfg.hostname, lease_file)
        state.status = "waiting-for-dhcp"
        state_file.write(_status_payload(cfg, state))
        ip = net.wait_for_ip(timeout=float(_env("DHCP_TIMEOUT", "45")))
        if not ip:
            state.status = "error"
            state.message = "No DHCP lease on the macvlan interface"
            state_file.write(_status_payload(cfg, state))
            print("[camera] no DHCP lease; check the macvlan parent interface")
            # Stay alive so the UI can report the problem instead of crash-looping.
            _idle(state_file, cfg, state)
            return 1

    state.refresh_from_interface()
    print(f"[camera] address {state.ip}/{state.prefix} via {state.gateway or 'no gateway'}")

    # 2. RTSP relay -------------------------------------------------------
    relay_proc = None
    if cfg.proxy:
        paths = {"main": cfg.source_url}
        if cfg.source_url_sub:
            paths["sub"] = cfg.source_url_sub
        config_path = mediamtx.write_config(paths, cfg.rtsp_port, cfg.rtsp_transport)
        relay_proc = mediamtx.start(config_path)
        if relay_proc is None:
            cfg.proxy = False  # fall back to handing out the upstream URL

    # 3. throughput + stream detection ------------------------------------
    collector = None
    if cfg.proxy:
        collector = StatsCollector()
        collector.start()

    # Probing opens a short connection to the real camera, so keep it off the
    # startup path: ONVIF must answer immediately, with the configured values
    # until the probe replaces them.
    threading.Thread(
        target=lambda: (_apply_probe(cfg, state), _apply_probe(cfg, state, sub=True)),
        name="probe",
        daemon=True,
    ).start()

    # 4. ONVIF + discovery ------------------------------------------------
    httpd = serve_onvif(cfg, state)
    discovery = DiscoveryResponder(cfg, state)
    discovery.start()

    state.status = "running"
    state.message = ""
    state_file.write(_status_payload(cfg, state))
    print(f"[camera] ONVIF ready at http://{state.ip}:{cfg.onvif_port}/onvif/device_service")

    # 5. supervise --------------------------------------------------------
    stopping = threading.Event()

    def _shutdown(signum, frame):
        print(f"[camera] signal {signum}, shutting down")
        stopping.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stopping.wait(HEARTBEAT_SECONDS):
        previous_ip = state.ip
        state.refresh_from_interface()
        if state.ip and state.ip != previous_ip:
            print(f"[camera] address changed {previous_ip or 'none'} -> {state.ip}")
        if relay_proc is not None and relay_proc.poll() is not None:
            print("[camera] RTSP relay exited; restarting it")
            relay_proc = mediamtx.start()
        if dhcp_proc is not None and dhcp_proc.poll() is not None:
            print("[camera] DHCP client exited; restarting it")
            dhcp_proc = net.start_dhcp(cfg.hostname, "/tmp/lease.env")

        if collector is not None:
            state.stats = collector.snapshot()
            # RTSP sources rarely declare a bitrate, so fall back to the
            # measured one for the value ONVIF advertises.
            measured = state.stats.get("measured_kbps", 0)
            if cfg.autodetect and measured and not _probed_bitrate(state):
                cfg.bitrate = measured

        state.status = "running" if state.ip else "no-address"
        state_file.write(_status_payload(cfg, state))

    if collector is not None:
        collector.stop()
    discovery.stop()
    httpd.shutdown()
    for proc in (relay_proc, dhcp_proc):
        if proc is not None and proc.poll() is None:
            proc.terminate()
    state.status = "stopped"
    state_file.write(_status_payload(cfg, state))
    return 0


def _idle(state_file: StateFile, cfg: Config, state: State):
    """Keep reporting an error state instead of exiting into a restart loop."""
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.wait(HEARTBEAT_SECONDS):
        state_file.write(_status_payload(cfg, state))


if __name__ == "__main__":
    sys.exit(main())
