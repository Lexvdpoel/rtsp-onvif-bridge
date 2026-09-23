"""Shared camera model + defaults used by both the controller and the camera role."""

from __future__ import annotations

import hashlib
import re
import time
import uuid

# Locally administered, unicast OUI prefix. Kept away from Docker's own 02:42
# range so camera MACs are easy to spot in the DHCP server's lease table.
MAC_PREFIX = "02:1f"

_SAFE_HOSTNAME = re.compile(r"[^a-zA-Z0-9-]+")

DEFAULTS = {
    "name": "Camera",
    "source_url": "",
    "source_url_sub": "",
    "enabled": True,
    "onvif_port": 80,
    "rtsp_port": 554,
    "username": "admin",
    "password": "admin",
    "require_auth": True,
    "proxy": True,
    "rtsp_transport": "tcp",
    "manufacturer": "RTSP-ONVIF-Bridge",
    "model": "VirtualCam",
    "firmware": "1.0.0",
    "width": 1920,
    "height": 1080,
    "fps": 15,
    "bitrate": 4096,
    "width_sub": 640,
    "height_sub": 360,
    "fps_sub": 15,
    "bitrate_sub": 512,
    "snapshot_enabled": True,
    "location": "any",
}


def generate_mac(cam_id: str) -> str:
    """Deterministic MAC for a camera id, so DHCP reservations survive recreation."""
    digest = hashlib.sha256(cam_id.encode("utf-8")).hexdigest()
    tail = [digest[i : i + 2] for i in range(0, 8, 2)]
    return ":".join(MAC_PREFIX.split(":") + tail)


def hostname_for(cam: dict) -> str:
    """DHCP hostname; this is what shows up in the UniFi / router client list."""
    base = _SAFE_HOSTNAME.sub("-", cam.get("name") or "camera").strip("-").lower()
    base = base or "camera"
    return f"{base}-{cam['id'][:6]}"[:63]


def container_name(cam: dict) -> str:
    return f"onvif-cam-{cam['id'][:12]}"


def serial_for(cam_id: str) -> str:
    return hashlib.sha256(("serial:" + cam_id).encode()).hexdigest()[:12].upper()


def new_camera(payload: dict | None = None) -> dict:
    cam = dict(DEFAULTS)
    cam["id"] = uuid.uuid4().hex
    cam["created_at"] = time.time()
    if payload:
        cam.update(sanitize(payload))
    cam["mac"] = generate_mac(cam["id"])
    cam["serial"] = serial_for(cam["id"])
    cam["uuid"] = str(uuid.UUID(hashlib.sha1(cam["id"].encode()).hexdigest()[:32]))
    return cam


_INT_FIELDS = {
    "onvif_port",
    "rtsp_port",
    "width",
    "height",
    "fps",
    "bitrate",
    "width_sub",
    "height_sub",
    "fps_sub",
    "bitrate_sub",
}
_BOOL_FIELDS = {"enabled", "require_auth", "proxy", "snapshot_enabled"}
_IMMUTABLE = {"id", "mac", "serial", "uuid", "created_at"}


def sanitize(payload: dict) -> dict:
    """Coerce a web-form payload into the stored shape; drops unknown keys."""
    out: dict = {}
    for key, value in payload.items():
        if key in _IMMUTABLE or key not in DEFAULTS:
            continue
        if key in _INT_FIELDS:
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                continue
        elif key in _BOOL_FIELDS:
            out[key] = value in (True, "true", "True", "on", 1, "1")
        else:
            out[key] = str(value).strip()
    return out


def validate(cam: dict) -> list[str]:
    errors = []
    if not cam.get("name"):
        errors.append("Name is required.")
    src = cam.get("source_url", "")
    if not src:
        errors.append("Source RTSP URL is required.")
    elif not src.startswith(("rtsp://", "rtsps://", "http://", "https://")):
        errors.append("Source URL must start with rtsp://, rtsps:// or http://.")
    if not 1 <= int(cam.get("onvif_port", 80)) <= 65535:
        errors.append("ONVIF port must be between 1 and 65535.")
    if not 1 <= int(cam.get("rtsp_port", 554)) <= 65535:
        errors.append("RTSP port must be between 1 and 65535.")
    if cam.get("require_auth") and not cam.get("password"):
        errors.append("A password is required when authentication is enabled.")
    return errors


def env_for(cam: dict, state_dir: str = "/state") -> dict:
    """Environment handed to the per-camera container."""
    return {
        "ROLE": "camera",
        "STATE_DIR": state_dir,
        "CAM_ID": cam["id"],
        "CAM_NAME": cam["name"],
        "CAM_UUID": cam["uuid"],
        "CAM_HOSTNAME": hostname_for(cam),
        "CAM_MAC": cam["mac"],
        "CAM_SERIAL": cam["serial"],
        "CAM_MANUFACTURER": cam["manufacturer"],
        "CAM_MODEL": cam["model"],
        "CAM_FIRMWARE": cam["firmware"],
        "CAM_LOCATION": cam.get("location") or "any",
        "SOURCE_URL": cam["source_url"],
        "SOURCE_URL_SUB": cam.get("source_url_sub", ""),
        "ONVIF_PORT": str(cam["onvif_port"]),
        "RTSP_PORT": str(cam["rtsp_port"]),
        "ONVIF_USER": cam["username"],
        "ONVIF_PASS": cam["password"],
        "REQUIRE_AUTH": "1" if cam["require_auth"] else "0",
        "PROXY": "1" if cam["proxy"] else "0",
        "RTSP_TRANSPORT": cam.get("rtsp_transport") or "tcp",
        "SNAPSHOT": "1" if cam["snapshot_enabled"] else "0",
        "VIDEO_WIDTH": str(cam["width"]),
        "VIDEO_HEIGHT": str(cam["height"]),
        "VIDEO_FPS": str(cam["fps"]),
        "VIDEO_BITRATE": str(cam["bitrate"]),
        "VIDEO_WIDTH_SUB": str(cam["width_sub"]),
        "VIDEO_HEIGHT_SUB": str(cam["height_sub"]),
        "VIDEO_FPS_SUB": str(cam["fps_sub"]),
        "VIDEO_BITRATE_SUB": str(cam["bitrate_sub"]),
    }
