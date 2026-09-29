"""UniFi camera model identities used during discovery.

Protect decides what a device is from the platform string and the system id it
announces, so emulating a camera means announcing a real pair. These values are
taken from the model table in the unifi-cam-proxy-redalert fork (MIT); they are
not guessed.

Which model to claim matters in practice. Protect gates features on it: a G4 line
implies H.264, a G5 line implies H.265. Pick one whose capabilities match the
stream you are actually feeding it.
"""

from __future__ import annotations

# model -> (platform, system id)
MODELS: dict[str, tuple[str, str]] = {
    # G3
    "UVC_G3": ("s2l", "0xa531"),
    "UVC_G3_DOME": ("s2l", "0xa533"),
    "UVC_G3_FLEX": ("s2l", "0xa534"),
    "UVC_G3_MICRO": ("s2lm", "0xa552"),
    "UVC_G3_INSTANT": ("sav532q", "0xa590"),
    "UVC_G3_PRO": ("s2l", "0xa532"),
    # G4
    "UVC_G4_BULLET": ("s5l", "0xa572"),
    "UVC_G4_DOME": ("s5l", "0xa573"),
    "UVC_G4_PRO": ("s5l", "0xa563"),
    "UVC_G4_PTZ": ("s5l", "0xa564"),
    "UVC_G4_INSTANT": ("sav530q", "0xa595"),
    "UVC_G4_DOORBELL": ("s5l", "0xa571"),
    # G5
    "UVC_G5_BULLET": ("sav530q", "0xa591"),
    "UVC_G5_DOME": ("sav530q", "0xa592"),
    "UVC_G5_FLEX": ("sav530q", "0xa593"),
    "UVC_G5_PRO": ("sav837gw", "0xa598"),
    "UVC_G5_TURRET_ULTRA": ("sav530q", "0xa59c"),
    "UVC_G5_DOME_ULTRA": ("sav530q", "0xa59d"),
    # G6
    "UVC_G6_BULLET": ("sav539g", "0xa600"),
    "UVC_G6_TURRET": ("sav539g", "0xa601"),
}

DEFAULT_MODEL = "UVC_G4_BULLET"
DEFAULT_FIRMWARE = "4.71.0"


def identity(model: str) -> dict:
    """Platform, system id and display name for a model, falling back sensibly."""
    name = (model or DEFAULT_MODEL).strip().upper().replace("-", "_").replace(" ", "_")
    platform, sysid = MODELS.get(name, MODELS[DEFAULT_MODEL])
    if name not in MODELS:
        name = DEFAULT_MODEL
    return {
        "model": name,
        "platform": platform,
        "sysid": sysid,
        "display": name.replace("UVC_", "").replace("_", " ").title(),
    }


def choices() -> list[str]:
    return sorted(MODELS)
