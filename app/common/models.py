"""Shared camera model + defaults used by both the controller and the camera role."""

from __future__ import annotations

import hashlib
import re
import shlex
import time
import uuid
from urllib.parse import urlsplit

# Locally administered, unicast OUI prefix. Kept away from Docker's own 02:42
# range so camera MACs are easy to spot in the DHCP server's lease table.
MAC_PREFIX = "02:1f"

_SAFE_HOSTNAME = re.compile(r"[^a-zA-Z0-9-]+")

DEFAULTS = {
    "name": "Camera",
    "source_url": "",
    "source_url_sub": "",
    # The real camera's MAC. Optional, but the most durable thing to derive the
    # virtual MAC from: it survives the source changing IP or password.
    "source_mac": "",
    "enabled": True,
    "onvif_port": 80,
    "rtsp_port": 554,
    "username": "admin",
    "password": "admin",
    "require_auth": True,
    "proxy": True,
    "rtsp_transport": "tcp",
    # Codec handed to the NVR. "copy" relays whatever the source sends; h264 or
    # h265 re-encode only when the source is not already that codec.
    "output_codec": "copy",
    "hwaccel": "auto",
    "encode_bitrate": 4096,
    "encode_preset": "veryfast",
    "audio": "copy",
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
    # On-camera object detection, run on the sub stream.
    "detect": False,
    "detect_types": "person,vehicle,animal",
    "detect_fps": 3,
    "detect_confidence": 0.5,
    "detect_min_hits": 3,
    "detect_cooldown": 30,
    # How long motion stays true after the last detection. An NVR that only
    # ever sees motion begin shows a camera that has been moving since it was
    # plugged in, so the state has to end as well as start.
    "event_hold": 8,
    "autodetect": True,
    "location": "any",
}


def generate_mac(identity: str, prefix: str = MAC_PREFIX) -> str:
    """Deterministic MAC for an identity string.

    Same identity in, same MAC out, so a camera that is deleted and added again
    lands back on its existing DHCP reservation.
    """
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    parts = prefix.split(":")
    tail = [digest[i : i + 2] for i in range(0, 2 * (6 - len(parts)), 2)]
    return ":".join(parts + tail)


def normalize_mac(value: str) -> str:
    """Twelve lowercase hex digits, or '' if this is not a MAC address."""
    digits = re.sub(r"[^0-9a-fA-F]", "", str(value or ""))
    return digits.lower() if len(digits) == 12 else ""


def identity_key(cam: dict) -> str:
    """What this camera's virtual MAC is derived from.

    The real camera's MAC is the best answer, because it survives the source
    changing address or password. Without it, fall back to the host and path of
    the source URL: credentials are stripped, so rotating a password does not
    move the camera onto a different address, and the path keeps two channels
    behind one recorder apart.
    """
    real = normalize_mac(cam.get("source_mac", ""))
    if real:
        return f"mac:{real}"

    url = (cam.get("source_url") or "").strip()
    if url:
        parsed = urlsplit(url)
        # netloc carries user:pass@host:port; hostname/port drop the credentials.
        host = (parsed.hostname or "").lower()
        port = f":{parsed.port}" if parsed.port else ""
        if host:
            return f"url:{host}{port}{parsed.path.rstrip('/')}".lower()

    # Nothing stable to go on; the id at least keeps it unique.
    return f"id:{cam.get('id', '')}"


def assign_mac(cam: dict, taken: set[str] | None = None) -> str:
    """Pick this camera's MAC, avoiding one already in use by another camera."""
    identity = identity_key(cam)
    prefix = MAC_PREFIX
    mac = generate_mac(identity, prefix)
    taken = {m.lower() for m in (taken or set())}
    # Two records pointing at the same source would otherwise collide, and two
    # NICs sharing a MAC on one LAN break both of them.
    suffix = 0
    while mac.lower() in taken:
        suffix += 1
        mac = generate_mac(f"{identity}#{suffix}", prefix)
    return mac


def hostname_for(cam: dict) -> str:
    """DHCP hostname; this is what shows up in the UniFi / router client list."""
    base = _SAFE_HOSTNAME.sub("-", cam.get("name") or "camera").strip("-").lower()
    base = base or "camera"
    return f"{base}-{cam['id'][:6]}"[:63]


def container_name(cam: dict) -> str:
    return f"onvif-cam-{cam['id'][:12]}"


def serial_for(cam_id: str) -> str:
    return hashlib.sha256(("serial:" + cam_id).encode()).hexdigest()[:12].upper()


def uuid_for(cam_id: str) -> str:
    """Stable ONVIF device UUID; a restored camera keeps its identity."""
    return str(uuid.UUID(hashlib.sha1(cam_id.encode()).hexdigest()[:32]))


def new_camera(payload: dict | None = None, taken: set[str] | None = None) -> dict:
    cam = dict(DEFAULTS)
    cam["id"] = uuid.uuid4().hex
    cam["created_at"] = time.time()
    if payload:
        cam.update(sanitize(payload))
    cam["mac"] = assign_mac(cam, taken)
    cam["serial"] = serial_for(cam["id"])
    cam["uuid"] = uuid_for(cam["id"])
    return cam


_INT_FIELDS = {
    "detect_fps",
    "detect_min_hits",
    "detect_cooldown",
    "event_hold",
    "encode_bitrate",
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
_FLOAT_FIELDS = {"detect_confidence"}
_BOOL_FIELDS = {
    "enabled", "require_auth", "proxy", "snapshot_enabled", "autodetect", "detect",
}
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
        elif key in _FLOAT_FIELDS:
            try:
                out[key] = float(value)
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
    if cam.get("detect"):
        if not 1 <= int(cam.get("detect_fps", 3)) <= 15:
            errors.append("Detection frame rate must be between 1 and 15.")
        if not 0.1 <= float(cam.get("detect_confidence", 0.5)) <= 0.99:
            errors.append("Detection confidence must be between 0.1 and 0.99.")
        if not 1 <= int(cam.get("event_hold", 8)) <= 300:
            errors.append("Motion hold must be between 1 and 300 seconds.")
        wanted = {t.strip() for t in (cam.get("detect_types") or "").split(",") if t.strip()}
        if not wanted:
            errors.append("Pick at least one object type to detect.")
        elif not wanted <= {"person", "vehicle", "animal"}:
            errors.append(
                "Detectable types are person, vehicle and animal. "
                "The detection model has no class for a package."
            )
    if cam.get("source_mac") and not normalize_mac(cam["source_mac"]):
        errors.append("Source MAC must be 12 hex digits, e.g. a0:bb:3e:11:22:33.")
    if cam.get("output_codec") not in ("copy", "h264", "h265"):
        errors.append("Output codec must be copy, h264 or h265.")
    if cam.get("hwaccel") not in ("auto", "none", "vaapi", "qsv", "nvenc"):
        errors.append("Encoder must be auto, none, vaapi, qsv or nvenc.")
    if cam.get("audio") not in ("copy", "none"):
        errors.append("Audio must be copy or none.")
    if not 64 <= int(cam.get("encode_bitrate", 4096)) <= 100000:
        errors.append("Encoder bitrate must be between 64 and 100000 kbps.")
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
        "OUTPUT_CODEC": cam.get("output_codec") or "copy",
        "HWACCEL": cam.get("hwaccel") or "auto",
        "ENCODE_BITRATE": str(cam.get("encode_bitrate") or 4096),
        "ENCODE_PRESET": cam.get("encode_preset") or "veryfast",
        "AUDIO": cam.get("audio") or "copy",
        "SNAPSHOT": "1" if cam["snapshot_enabled"] else "0",
        "DETECT": "1" if cam.get("detect") else "0",
        "DETECT_TYPES": cam.get("detect_types") or "person",
        "DETECT_FPS": str(cam.get("detect_fps") or 3),
        "DETECT_CONFIDENCE": str(cam.get("detect_confidence") or 0.5),
        "DETECT_MIN_HITS": str(cam.get("detect_min_hits") or 3),
        "DETECT_COOLDOWN": str(cam.get("detect_cooldown") or 30),
        "EVENT_HOLD": str(cam.get("event_hold") or 8),
        "AUTODETECT": "1" if cam.get("autodetect", True) else "0",
        "VIDEO_WIDTH": str(cam["width"]),
        "VIDEO_HEIGHT": str(cam["height"]),
        "VIDEO_FPS": str(cam["fps"]),
        "VIDEO_BITRATE": str(cam["bitrate"]),
        "VIDEO_WIDTH_SUB": str(cam["width_sub"]),
        "VIDEO_HEIGHT_SUB": str(cam["height_sub"]),
        "VIDEO_FPS_SUB": str(cam["fps_sub"]),
        "VIDEO_BITRATE_SUB": str(cam["bitrate_sub"]),
    }
