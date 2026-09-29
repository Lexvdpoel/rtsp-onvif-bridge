"""Entrypoint for a single virtual ONVIF camera container.

Boot order:
  1. take a DHCP lease on the macvlan interface (own MAC -> own IP)
  2. serve ONVIF over HTTP and answer WS-Discovery probes, so the device is
     reachable straight away
  3. probe the source, which decides whether a transcode is needed
  4. start the RTSP relay, pulling the source or re-encoding it
  5. keep a small status file the web UI reads
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass

from . import (
    detect as detect_mod,
    mediamtx,
    net,
    probe as probe_mod,
    transcode,
    unifi,
    unifi_adopt,
    unifi_discovery,
    unifi_models,
)
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
    detect: bool
    detect_types: str
    detect_fps: int
    detect_confidence: float
    detect_min_hits: int
    detect_cooldown: int
    width: int
    height: int
    fps: int
    bitrate: int
    width_sub: int
    height_sub: int
    fps_sub: int
    bitrate_sub: int
    mode: str
    unifi_host: str
    unifi_token: str
    unifi_extra_args: str
    unifi_discoverable: bool
    unifi_model: str
    unifi_firmware: str
    mac_hint: str
    output_codec: str
    hwaccel: str
    encode_bitrate: int
    encode_preset: str
    audio: str
    # Replaced by the probed codec when autodetect is on, or by the target
    # codec when the stream is re-encoded.
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
            detect=_env_bool("DETECT", False),
            detect_types=_env("DETECT_TYPES", "person,vehicle,animal"),
            detect_fps=_env_int("DETECT_FPS", 3),
            detect_confidence=float(_env("DETECT_CONFIDENCE", "0.5") or 0.5),
            detect_min_hits=_env_int("DETECT_MIN_HITS", 3),
            detect_cooldown=_env_int("DETECT_COOLDOWN", 30),
            width=_env_int("VIDEO_WIDTH", 1920),
            height=_env_int("VIDEO_HEIGHT", 1080),
            fps=_env_int("VIDEO_FPS", 15),
            bitrate=_env_int("VIDEO_BITRATE", 4096),
            width_sub=_env_int("VIDEO_WIDTH_SUB", 640),
            height_sub=_env_int("VIDEO_HEIGHT_SUB", 360),
            fps_sub=_env_int("VIDEO_FPS_SUB", 15),
            bitrate_sub=_env_int("VIDEO_BITRATE_SUB", 512),
            mode=_env("MODE", "onvif"),
            unifi_host=_env("UNIFI_HOST"),
            unifi_token=_env("UNIFI_TOKEN"),
            unifi_extra_args=_env("UNIFI_EXTRA_ARGS"),
            unifi_discoverable=_env_bool("UNIFI_DISCOVERABLE", True),
            unifi_model=_env("UNIFI_MODEL", "UVC_G4_BULLET"),
            unifi_firmware=_env("UNIFI_FIRMWARE", "4.71.0"),
            mac_hint=_env("CAM_MAC"),
            output_codec=_env("OUTPUT_CODEC", "copy"),
            hwaccel=_env("HWACCEL", "none"),
            encode_bitrate=_env_int("ENCODE_BITRATE", 4096),
            encode_preset=_env("ENCODE_PRESET", "veryfast"),
            audio=_env("AUDIO", "copy"),
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
        self.transcode: dict = {}
        self.detections: list = []
        self.unifi: dict = {}

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
        "transcode": state.transcode,
        "mode": cfg.mode,
        "unifi": state.unifi,
        "detections": state.detections[-20:],
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


def _relay_paths(cfg: Config, state: State) -> dict[str, dict]:
    """Decide per stream whether to relay it as-is or re-encode it.

    Also settles what ONVIF advertises: after a transcode the NVR receives the
    target codec, not the source's.
    """
    paths: dict[str, dict] = {}
    plans: dict[str, str] = {}

    for key, url, sub in (("main", cfg.source_url, False), ("sub", cfg.source_url_sub, True)):
        if not url:
            continue
        source_codec = (state.detected.get(key) or {}).get("codec", "")
        if transcode.needs_transcode(source_codec, cfg.output_codec):
            args = transcode.build_args(
                source_url=url,
                publish_url=f"rtsp://127.0.0.1:{cfg.rtsp_port}/{key}",
                output_codec=cfg.output_codec,
                hwaccel=cfg.hwaccel,
                bitrate_kbps=cfg.encode_bitrate if not sub else cfg.bitrate_sub,
                preset=cfg.encode_preset,
                transport=cfg.rtsp_transport,
                audio=cfg.audio,
            )
            script_path = mediamtx.write_transcode_script(key, args, transcode.script)
            paths[key] = {"script": script_path}
        else:
            paths[key] = {"source": url}
        plans[key] = transcode.describe(source_codec, cfg.output_codec, cfg.hwaccel)
        print(f"[relay] {key}: {plans[key]}")

    transcoding = any("script" in spec for spec in paths.values())
    if cfg.output_codec in ("h264", "h265"):
        # What leaves the bridge is the requested codec, whether it was
        # re-encoded or already matched.
        cfg.encoding = "H264" if cfg.output_codec == "h264" else "H265"
        if transcoding:
            cfg.bitrate = cfg.encode_bitrate

    state.transcode = {
        "active": transcoding,
        "output_codec": cfg.output_codec,
        "hwaccel": cfg.hwaccel,
        "plans": plans,
    }
    return paths


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
    if cfg.mac_hint and state.mac and state.mac.lower() != cfg.mac_hint.lower():
        # The daemon ignored the address it was given, so this camera is on a
        # random one. DHCP then treats it as a new device on every recreate and
        # the reservation never sticks, which is worth saying out loud.
        print(
            f"[camera] WARNING: asked Docker for MAC {cfg.mac_hint} but this "
            f"container has {state.mac}. The address was not applied, so the "
            "DHCP reservation for this camera will not be used.",
            file=sys.stderr,
        )
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

    # 2. ONVIF + discovery ------------------------------------------------
    # Brought up before the relay so the device answers straight away; the
    # stream URI it hands out works as soon as the relay follows. In UniFi mode
    # Protect speaks its own protocol, so none of this applies.
    httpd = None
    discovery = None
    if cfg.mode != "unifi":
        httpd = serve_onvif(cfg, state)
        discovery = DiscoveryResponder(cfg, state)
        discovery.start()
        print(
            f"[camera] ONVIF ready at "
            f"http://{state.ip}:{cfg.onvif_port}/onvif/device_service"
        )

    # 3. detect the source ------------------------------------------------
    # Whether a transcode is needed depends on the source codec, so this has to
    # finish before the relay is configured.
    state.status = "probing"
    state_file.write(_status_payload(cfg, state))
    _apply_probe(cfg, state)
    _apply_probe(cfg, state, sub=True)

    # 4. RTSP relay -------------------------------------------------------
    relay_proc = None
    if cfg.proxy:
        paths = _relay_paths(cfg, state)
        config_path = mediamtx.write_config(paths, cfg.rtsp_port, cfg.rtsp_transport)
        relay_proc = mediamtx.start(config_path)
        if relay_proc is None:
            cfg.proxy = False  # fall back to handing out the upstream URL

    collector = None
    if cfg.proxy:
        collector = StatsCollector()
        collector.start()

    # 4b. UniFi Protect ----------------------------------------------------
    unifi_proc = None
    unifi_args: list[str] = []
    discovery_responder = None
    adopt_server = None
    if cfg.mode == "unifi":
        # Point the proxy at our own relay when it is running, so the codec
        # conversion and hardware encoding still apply on the way to Protect.
        stream_url = (
            f"rtsp://127.0.0.1:{cfg.rtsp_port}/main" if cfg.proxy else cfg.source_url
        )
        state_dir = _env("STATE_DIR", "/state")
        try:
            cert = unifi.ensure_certificate(cfg.id, state_dir)
        except Exception as exc:  # noqa: BLE001
            cert = ""
            state.message = f"Could not prepare the UniFi certificate: {exc}"
            print(f"[unifi] {state.message}")

        def launch_proxy(token: str, host: str) -> None:
            nonlocal unifi_proc, unifi_args
            try:
                unifi_args = unifi.build_args(cfg, state, cert, stream_url,
                                              token=token, host=host)
                unifi_proc = unifi.start(unifi_args)
                state.message = ""
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                state.message = f"UniFi proxy failed to start: {exc}"
                print(f"[unifi] {state.message}")

        if cert:
            identity = unifi_models.identity(cfg.unifi_model)
            stored = unifi_adopt.load_payload(cfg.id, state_dir)
            # The discovery setting decides where the credentials come from.
            # Letting a leftover token quietly win would disable discovery
            # without anything saying so.
            if cfg.unifi_discoverable:
                token = stored.get("token", "")
                host = stored.get("host", "")
                if stored.get("port") and host and ":" not in host:
                    host = f"{host}:{stored['port']}"
            else:
                token = cfg.unifi_token
                host = cfg.unifi_host

            state.unifi = {
                "model": identity["model"],
                "adopted": bool(token and host),
                "console": host,
                "discoverable": cfg.unifi_discoverable,
            }

            if token and host:
                launch_proxy(token, host)
            elif cfg.unifi_discoverable:
                # Offer ourselves the way a factory camera does: answer the
                # discovery probe, then take the token Protect pushes to us.
                def adopted(payload: dict) -> None:
                    console = f"{payload['host']}:{payload['port']}"
                    state.unifi = dict(state.unifi, adopted=True, console=console)
                    print(f"[unifi] adopted by {console}; starting the proxy")
                    launch_proxy(payload["token"], console)

                service = unifi_adopt.AdoptionService(
                    cfg, state, identity, state_dir, on_adopted=adopted
                )
                try:
                    adopt_server = unifi_adopt.serve(service, cert)
                except Exception as exc:  # noqa: BLE001
                    state.message = f"Adoption endpoint failed to start: {exc}"
                    print(f"[unifi] {state.message}")
                discovery_responder = unifi_discovery.DiscoveryResponder(
                    cfg, state, identity,
                    adoptable=lambda: not service.adopted.is_set(),
                    # Known console address: announce to it as well, so a
                    # console behind a router still sees the camera.
                    console=cfg.unifi_host,
                )
                discovery_responder.start()
                print(
                    "[unifi] waiting to be adopted; the camera should appear in "
                    "Protect under devices ready to adopt"
                )
            else:
                state.message = (
                    "UniFi mode needs either discovery or an adoption token."
                )
                print(f"[unifi] {state.message}")

    # 4c. object detection ------------------------------------------------
    detector = None
    if cfg.detect:
        sink = detect_mod.UnifiSink(_env_int("DETECT_BRIDGE_PORT", 8099)) \
            if cfg.mode == "unifi" else None

        def on_detection(object_type: str, score: float):
            # Always recorded so the bridge's own UI can show it; in UniFi mode
            # it is also handed to the proxy, which reports it to Protect.
            state.detections.append(
                {"type": object_type, "score": round(score, 3), "at": time.time()}
            )
            del state.detections[:-50]
            if sink is not None:
                sink(object_type, score)

        detector = detect_mod.Detector(cfg, on_detection)
        detector.start()

    state.status = "running"
    state.message = ""
    state_file.write(_status_payload(cfg, state))

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
        if unifi_proc is not None and unifi_proc.poll() is not None:
            print("[unifi] proxy exited; restarting it")
            unifi_proc = unifi.start(unifi_args)
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

    if discovery_responder is not None:
        discovery_responder.stop()
    if adopt_server is not None:
        adopt_server.shutdown()
    if detector is not None:
        detector.stop()
    if collector is not None:
        collector.stop()
    if discovery is not None:
        discovery.stop()
    if httpd is not None:
        httpd.shutdown()
    for proc in (relay_proc, dhcp_proc, unifi_proc):
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
