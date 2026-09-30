"""Web UI and REST API for managing the virtual ONVIF cameras."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool

from ..common import models
from . import backup as backup_mod
from .auth import COOKIE_NAME, SESSION_DAYS, AuthStore
from .clip_store import GIGABYTE, ClipStore
from .docker_mgr import DockerError, DockerManager
from . import mjpeg
from .hwdetect import HardwareDetector, summarize
from .settings import SettingsStore
from .store import CameraStore

DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

# A camera writes its state file every 10s; anything older means it went away.
STATE_STALE_SECONDS = 45

store = CameraStore(DATA_DIR)
auth = AuthStore(DATA_DIR)
settings = SettingsStore(DATA_DIR)
clip_store = ClipStore(STATE_DIR)
manager: DockerManager | None = None
detector: HardwareDetector | None = None
startup_error: str = ""

# Reachable before signing in: the page shell itself, and the endpoints the
# login and first-run screens need in order to work.
PUBLIC_PATHS = {
    "/",
    "/index.html",
    "/api/auth/state",
    "/api/auth/setup",
    "/api/auth/login",
    "/favicon.ico",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager, detector, startup_error
    try:
        manager = DockerManager()
        manager.ensure_network()
    except DockerError as exc:
        startup_error = str(exc)
        print(f"[controller] {exc}")
    else:
        detector = HardwareDetector(manager.client, manager.image, DATA_DIR)
        manager.detector = detector
        # Detecting starts containers, so keep it off the startup path; cameras
        # created before it finishes fall back to software encoding.
        threading.Thread(target=_detect_hardware, name="hwdetect", daemon=True).start()
        threading.Thread(target=_autostart, name="autostart", daemon=True).start()
    threading.Thread(target=_prune_clips, name="clips", daemon=True).start()
    yield


# Often enough that a burst cannot overshoot the budget by much, rarely enough
# that walking the store is not a constant cost.
PRUNE_SECONDS = 120


def _prune_clips():
    """Keep the detection stills inside the configured budget, for ever."""
    while True:
        try:
            budget = settings.all()["clip_budget_gb"]
            clip_store.prune(int(budget * GIGABYTE))
        except Exception as exc:  # noqa: BLE001 - a full disk must not be fatal
            print(f"[clips] pruning failed: {exc}")
        time.sleep(PRUNE_SECONDS)


def _detect_hardware():
    try:
        report = detector.ensure()
        print(f"[controller] hardware encoders: {summarize(report)}")
    except Exception as exc:  # noqa: BLE001 - never fatal
        print(f"[controller] hardware detection failed: {exc}")


def _migrate_macs():
    """Move cameras off the Ubiquiti OUI an earlier version gave them.

    UniFi mode needed a MAC that looked like Ubiquiti hardware. That mode is
    gone, and an address from a real manufacturer's range is now only a way to
    collide with their equipment. Runs once: after the rewrite every address is
    already in range, so there is nothing left to match.

    This changes the address a camera takes its DHCP lease on, so it is said
    plainly rather than done quietly -- the reservation has to be moved with it.
    """
    stale = [cam for cam in store.list()
             if not (cam.get("mac") or "").lower().startswith(models.MAC_PREFIX)]
    if not stale:
        return
    taken = {cam["mac"] for cam in store.list()}
    for cam in stale:
        taken.discard(cam["mac"])
        updated = dict(cam, mac=models.assign_mac(cam, taken))
        taken.add(updated["mac"])
        store.upsert(updated)
        print(
            f"[controller] '{cam['name']}' moved from {cam['mac']} to "
            f"{updated['mac']}: the Ubiquiti address it had belonged to UniFi "
            "mode, which no longer exists. Update its DHCP reservation."
        )
        if manager is not None:
            try:
                manager.remove(updated)
            except DockerError as exc:
                print(f"[controller] could not recreate '{cam['name']}': {exc}")


def _autostart():
    """Bring up every camera marked enabled, so a host reboot restores them."""
    _migrate_macs()
    for cam in store.list():
        if not cam.get("enabled"):
            continue
        try:
            state = manager.status(cam)
            if state["state"] != "running":
                manager.start(cam)
                print(f"[controller] started '{cam['name']}'")
        except DockerError as exc:
            print(f"[controller] autostart of '{cam['name']}' failed: {exc}")


app = FastAPI(title="RTSP to ONVIF bridge", lifespan=lifespan)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Everything but the login surface needs a valid session cookie."""
    path = request.url.path
    if path in PUBLIC_PATHS or request.method == "OPTIONS":
        return await call_next(request)
    if auth.valid_token(request.cookies.get(COOKIE_NAME, "")):
        return await call_next(request)

    detail = (
        "Set a username and password first."
        if not auth.configured
        else "Sign in to continue."
    )
    return JSONResponse({"detail": detail, "auth_required": True}, status_code=401)


def _set_session(response: JSONResponse) -> JSONResponse:
    response.set_cookie(
        COOKIE_NAME,
        auth.issue_token(),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


# ------------------------------------------------------------------------ auth


@app.get("/api/auth/state")
def auth_state(request: Request):
    return {
        "configured": auth.configured,
        "authenticated": auth.valid_token(request.cookies.get(COOKIE_NAME, "")),
        "username": auth.username(),
    }


@app.post("/api/auth/setup")
async def auth_setup(request: Request):
    if auth.configured:
        return JSONResponse(
            {"detail": "An account already exists; sign in instead."}, status_code=409
        )
    payload = await request.json()
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if len(username) < 3:
        return JSONResponse(
            {"detail": "The username needs at least 3 characters."}, status_code=400
        )
    if len(password) < 8:
        return JSONResponse(
            {"detail": "The password needs at least 8 characters."}, status_code=400
        )

    await run_in_threadpool(auth.set_credentials, username, password)
    return _set_session(JSONResponse({"configured": True, "username": username}))


@app.post("/api/auth/login")
async def auth_login(request: Request):
    if not auth.configured:
        return JSONResponse(
            {"detail": "No account exists yet."}, status_code=409
        )
    payload = await request.json()
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not await run_in_threadpool(auth.verify, username, password):
        return JSONResponse(
            {"detail": "Wrong username or password."}, status_code=401
        )
    return _set_session(JSONResponse({"authenticated": True, "username": username}))


@app.post("/api/auth/logout")
def auth_logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.post("/api/auth/password")
async def auth_password(request: Request):
    payload = await request.json()
    current = str(payload.get("current", ""))
    new = str(payload.get("new", ""))
    if not await run_in_threadpool(auth.verify, auth.username(), current):
        return JSONResponse({"detail": "The current password is wrong."}, status_code=403)
    if len(new) < 8:
        return JSONResponse(
            {"detail": "The new password needs at least 8 characters."}, status_code=400
        )
    # This invalidates every existing session, including this one.
    await run_in_threadpool(auth.set_credentials, auth.username(), new)
    return _set_session(JSONResponse({"changed": True}))


# --------------------------------------------------------------------- helpers


def _require_manager() -> DockerManager:
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail=startup_error or "Docker is not available in this container.",
        )
    return manager


def _runtime_state(cam_id: str) -> dict:
    path = os.path.join(STATE_DIR, f"{cam_id}.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if time.time() - data.get("updated_at", 0) > STATE_STALE_SECONDS:
        data["stale"] = True
    return data


def _decorate(cam: dict) -> dict:
    item = dict(cam)
    item.pop("password", None)
    item["has_password"] = bool(cam.get("password"))
    item["hostname"] = models.hostname_for(cam)
    item["container"] = models.container_name(cam)
    item["runtime"] = _runtime_state(cam["id"])
    if manager is not None:
        item["resolved_hwaccel"] = manager.resolved_hwaccel(cam)
        try:
            item["docker"] = manager.status(cam)
        except Exception as exc:  # noqa: BLE001
            item["docker"] = {"exists": False, "state": "unknown", "error": str(exc)}
    else:
        item["docker"] = {"exists": False, "state": "unavailable"}
    return item


def _get_or_404(cam_id: str) -> dict:
    cam = store.get(cam_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return cam


# ------------------------------------------------------------------------ views


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(TEMPLATE_DIR, "index.html"), encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@app.get("/api/status")
def status():
    info = {
        "docker": manager is not None and manager.ping(),
        "error": startup_error,
        "network": {},
        "image": os.environ.get("BRIDGE_IMAGE", "rtsp-onvif-bridge:latest"),
    }
    if manager is not None:
        info["network"] = {
            "name": manager.network_name,
            "parent": manager.parent,
            "subnet": manager.subnet,
        }
        try:
            info["network"].update(manager.ensure_network())
        except DockerError as exc:
            info["error"] = str(exc)
    return info


@app.get("/api/cameras")
def list_cameras():
    return [_decorate(cam) for cam in store.list()]


@app.post("/api/cameras")
async def create_camera(request: Request):
    payload = await request.json()
    draft = dict(models.DEFAULTS)
    draft.update(models.sanitize(payload))
    errors = models.validate(draft)
    if errors:
        return JSONResponse({"detail": " ".join(errors)}, status_code=400)
    # Creating a container talks to the Docker daemon and can take seconds, so
    # it runs off the event loop; otherwise every other request waits on it.
    return await run_in_threadpool(_create_camera_sync, payload)


def _create_camera_sync(payload: dict):
    cam = store.add(payload)
    if cam.get("enabled"):
        try:
            _require_manager().start(cam)
        except (DockerError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return JSONResponse(
                {"camera": _decorate(cam), "warning": detail}, status_code=202
            )
    return _decorate(cam)


@app.put("/api/cameras/{cam_id}")
async def update_camera(cam_id: str, request: Request):
    cam = _get_or_404(cam_id)
    payload = await request.json()

    draft = dict(cam)
    draft.update(models.sanitize(payload))
    errors = models.validate(draft)
    if errors:
        return JSONResponse({"detail": " ".join(errors)}, status_code=400)
    return await run_in_threadpool(_update_camera_sync, cam_id, payload)


def _update_camera_sync(cam_id: str, payload: dict):
    updated = store.update(cam_id, payload)
    mgr = _require_manager()
    try:
        if updated.get("enabled"):
            # Recreate so the new environment is actually applied.
            mgr.restart(updated)
        else:
            mgr.remove(updated)
    except DockerError as exc:
        return JSONResponse(
            {"camera": _decorate(updated), "warning": str(exc)}, status_code=202
        )
    return _decorate(updated)


@app.delete("/api/cameras/{cam_id}")
def delete_camera(cam_id: str):
    cam = _get_or_404(cam_id)
    if manager is not None:
        try:
            manager.remove(cam)
            manager.purge(cam)
        except DockerError as exc:
            print(f"[controller] cleanup of '{cam['name']}' failed: {exc}")
    store.delete(cam_id)
    # The stills outlive the container otherwise, and nothing would ever come
    # back for them.
    clip_store.forget(cam_id)
    return {"deleted": cam_id}


@app.post("/api/cameras/{cam_id}/{action}")
def camera_action(cam_id: str, action: str):
    cam = _get_or_404(cam_id)
    mgr = _require_manager()
    try:
        if action == "start":
            store.update(cam_id, {"enabled": True})
            mgr.start(store.get(cam_id))
        elif action == "stop":
            store.update(cam_id, {"enabled": False})
            mgr.stop(cam)
        elif action == "restart":
            mgr.restart(cam)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action '{action}'")
    except DockerError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _decorate(store.get(cam_id))


@app.get("/api/cameras/{cam_id}/logs", response_class=PlainTextResponse)
def camera_logs(cam_id: str, tail: int = 200):
    cam = _get_or_404(cam_id)
    return _require_manager().logs(cam, tail=tail)


@app.get("/api/cameras/{cam_id}/password")
def camera_password(cam_id: str):
    """Returned only on explicit request, so the list view stays free of secrets."""
    cam = _get_or_404(cam_id)
    return {"password": cam.get("password", "")}


@app.get("/api/hwaccel")
def hwaccel_state():
    if detector is None:
        return {"detected_at": 0, "summary": "Docker unavailable", "available": {}}
    report = dict(detector.cached())
    report["summary"] = summarize(report)
    return report


@app.post("/api/hwaccel/detect")
def hwaccel_detect():
    """Re-run the probe; the host's hardware or the image may have changed."""
    if detector is None:
        raise HTTPException(status_code=503, detail="Docker is not available.")
    try:
        report = dict(detector.detect())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    report["summary"] = summarize(report)
    return report


@app.get("/api/backup")
def download_backup():
    """Full camera configuration, including credentials, as a download."""
    payload = backup_mod.export(store.list())
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return JSONResponse(
        payload,
        headers={
            "Content-Disposition":
                f'attachment; filename="onvif-bridge-backup-{stamp}.json"'
        },
    )


@app.post("/api/restore")
async def restore_backup(request: Request):
    body = await request.json()
    document = body.get("backup", body)
    replace = bool(body.get("replace", True)) if isinstance(body, dict) else True
    # A restore recreates every camera container, so it must not hold the loop.
    return await run_in_threadpool(_restore_sync, document, replace)


def _restore_sync(document, replace: bool):
    try:
        cameras = backup_mod.parse(document)
    except backup_mod.RestoreError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    mgr = manager
    warnings: list[str] = []

    if replace:
        # Tear down what is running before the configuration underneath changes.
        for existing in store.list():
            if mgr is not None:
                try:
                    mgr.remove(existing)
                except DockerError as exc:
                    warnings.append(f"{existing['name']}: {exc}")
        store.replace_all(cameras)
    else:
        for cam in cameras:
            if mgr is not None:
                try:
                    mgr.remove(cam)
                except DockerError as exc:
                    warnings.append(f"{cam['name']}: {exc}")
            store.upsert(cam)

    started = 0
    for cam in cameras:
        if not cam.get("enabled"):
            continue
        if mgr is None:
            warnings.append("Docker is unavailable, so nothing was started.")
            break
        try:
            mgr.start(cam)
            started += 1
        except DockerError as exc:
            warnings.append(f"{cam['name']}: {exc}")

    return {
        "restored": len(cameras),
        "started": started,
        "replaced": replace,
        "warnings": warnings,
    }


@app.get("/api/orphans")
def list_orphans():
    mgr = _require_manager()
    known = {cam["id"] for cam in store.list()}
    return {"orphans": mgr.orphans(known)}


# ------------------------------------------------------------ detection stills


@app.get("/api/cameras/{cam_id}/events")
def camera_event_days(cam_id: str):
    """Which days this camera has stills for, and how big the store is."""
    _get_or_404(cam_id)
    budget = settings.all()["clip_budget_gb"]
    return {
        "days": clip_store.days(cam_id),
        "usage": clip_store.last_prune or {"budget": int(budget * GIGABYTE)},
    }


@app.get("/api/cameras/{cam_id}/events/{day}")
def camera_events(cam_id: str, day: str):
    _get_or_404(cam_id)
    return {"day": day, "events": clip_store.events(cam_id, day)}


@app.get("/api/cameras/{cam_id}/events/{day}/{name}")
def camera_event_image(cam_id: str, day: str, name: str):
    _get_or_404(cam_id)
    path = clip_store.path_of(cam_id, day, name)
    if not path:
        raise HTTPException(status_code=404, detail="No such still")
    # Immutable once written, and named after the moment it was taken, so a
    # browser may keep it as long as it likes.
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000"})


# ------------------------------------------------------------------ live view


def _live_source(cam: dict, quality: str) -> str:
    """The relay path to read, on the camera's own address.

    "low" is the camera's sub stream where it has one: a grid of a dozen tiles
    wants the small picture, and asking every camera for its full resolution to
    show it at 300 pixels wide is the quickest way to run a host out of CPU.
    """
    runtime = _runtime_state(cam["id"])
    ip = runtime.get("ip")
    if not ip:
        return ""
    path = "sub" if quality == "low" and cam.get("source_url_sub") else "main"
    return f"rtsp://{ip}:{cam.get('rtsp_port', 554)}/{path}"


@app.get("/api/cameras/{cam_id}/live.mjpeg")
def camera_live(cam_id: str, quality: str = "low", fps: int = 6, width: int = 640):
    cam = _get_or_404(cam_id)
    source = _live_source(cam, quality)
    if not source:
        raise HTTPException(status_code=503, detail="This camera has no address yet")
    if not mjpeg.available():
        raise HTTPException(status_code=503, detail="ffmpeg is not installed")
    return StreamingResponse(
        mjpeg.frames(source, mjpeg.clamp_fps(fps), mjpeg.clamp_width(width)),
        media_type=mjpeg.CONTENT_TYPE,
        # A live stream that a proxy decides to cache is a still picture that
        # never changes, which is a confusing way to find out about a proxy.
        headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
    )


@app.get("/api/cameras/{cam_id}/live.jpg")
def camera_live_still(cam_id: str, quality: str = "low", width: int = 640):
    """One frame, for a tile to show before its stream has started."""
    cam = _get_or_404(cam_id)
    source = _live_source(cam, quality)
    image = mjpeg.still_frame(source, mjpeg.clamp_width(width)) if source else b""
    if not image:
        raise HTTPException(status_code=503, detail="No frame available")
    return Response(content=image, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------- global timeline


@app.get("/api/events")
def all_event_days():
    """Every day any camera has stills for, merged, newest first."""
    totals: dict = {}
    for cam in store.list():
        for entry in clip_store.days(cam["id"]):
            totals[entry["day"]] = totals.get(entry["day"], 0) + entry["count"]
    days = [{"day": day, "count": count} for day, count in totals.items()]
    days.sort(key=lambda d: d["day"], reverse=True)
    return {"days": days, "usage": clip_store.last_prune or {}}


@app.get("/api/events/{day}")
def all_events(day: str):
    """One day across every camera, in one sequence, oldest first."""
    out = []
    for cam in store.list():
        for event in clip_store.events(cam["id"], day):
            event["camera_id"] = cam["id"]
            event["camera"] = cam["name"]
            out.append(event)
    out.sort(key=lambda e: e["at"])
    return {"day": day, "events": out}


# --------------------------------------------------------------------- settings


@app.get("/api/settings")
def read_settings():
    current = dict(settings.all())
    current["clips"] = clip_store.last_prune or {}
    return current


@app.put("/api/settings")
async def write_settings(request: Request):
    payload = await request.json()
    current = await run_in_threadpool(settings.update, payload)
    # Applied at once rather than at the next sweep: someone who has just
    # lowered the budget wants to see the space come back.
    await run_in_threadpool(
        clip_store.prune, int(current["clip_budget_gb"] * GIGABYTE)
    )
    return {**current, "clips": clip_store.last_prune}


@app.delete("/api/orphans/{name}")
def remove_orphan(name: str):
    _require_manager().remove_orphan(name)
    return {"removed": name}
