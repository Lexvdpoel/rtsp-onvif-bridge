"""Backup and restore of the camera configuration.

A backup keeps each camera's id, and therefore its MAC address, so restoring
onto a fresh install brings the cameras back on the same DHCP reservations they
had before.
"""

from __future__ import annotations

import time

from ..common import models

FORMAT = "rtsp-onvif-bridge-backup"
VERSION = 1


def export(cameras: list[dict]) -> dict:
    return {
        "format": FORMAT,
        "version": VERSION,
        "created_at": time.time(),
        "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "camera_count": len(cameras),
        "cameras": cameras,
    }


class RestoreError(ValueError):
    pass


def parse(payload) -> list[dict]:
    """Validate a backup document and return the cameras it holds.

    A bare list of cameras is accepted too, so a hand-edited export still works.
    """
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict):
        if payload.get("format") not in (FORMAT, None):
            raise RestoreError(
                f"Not a {FORMAT} file (found format '{payload.get('format')}')."
            )
        version = payload.get("version", VERSION)
        if not isinstance(version, int) or version > VERSION:
            raise RestoreError(
                f"Backup version {version} is newer than this install supports "
                f"(up to {VERSION})."
            )
        raw = payload.get("cameras")
    else:
        raise RestoreError("The backup file must contain a JSON object or list.")

    if not isinstance(raw, list):
        raise RestoreError("The backup contains no 'cameras' list.")
    if not raw:
        raise RestoreError("The backup contains no cameras.")

    cameras = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RestoreError(f"Camera {index + 1} is not an object.")
        cameras.append(_normalize(item, index))

    ids = [cam["id"] for cam in cameras]
    if len(set(ids)) != len(ids):
        raise RestoreError("The backup contains duplicate camera ids.")
    return cameras


def _normalize(item: dict, index: int) -> dict:
    """Rebuild a stored camera from backup data, filling in what is missing."""
    cam = dict(models.DEFAULTS)
    cam.update(models.sanitize(item))

    cam_id = str(item.get("id") or "").strip()
    if not cam_id:
        raise RestoreError(f"Camera {index + 1} ('{cam.get('name')}') has no id.")
    cam["id"] = cam_id
    cam["created_at"] = item.get("created_at") or time.time()

    # Derived from the id, so a backup that lost them still restores the same
    # MAC address and therefore the same DHCP reservation.
    cam["mac"] = item.get("mac") or models.generate_mac(cam_id)
    cam["serial"] = item.get("serial") or models.serial_for(cam_id)
    cam["uuid"] = item.get("uuid") or models.uuid_for(cam_id)

    errors = models.validate(cam)
    if errors:
        raise RestoreError(
            f"Camera {index + 1} ('{cam.get('name') or 'unnamed'}'): {' '.join(errors)}"
        )
    return cam
