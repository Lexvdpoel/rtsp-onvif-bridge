"""Web UI and REST API for managing the virtual ONVIF cameras."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ..common import models
from . import backup as backup_mod
from .auth import COOKIE_NAME, SESSION_DAYS, AuthStore
from .docker_mgr import DockerError, DockerManager
from .hwdetect import HardwareDetector, summarize
from .store import CameraStore

DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

# A camera writes its state file every 10s; anything older means it went away.
STATE_STALE_SECONDS = 45

store = CameraStore(DATA_DIR)
auth = AuthStore(DATA_DIR)
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
        _autostart()
    yield


def _detect_hardware():
    try:
        report = detector.ensure()
        print(f"[controller] hardware encoders: {summarize(report)}")
    except Exception as exc:  # noqa: BLE001 - never fatal
        print(f"[controller] hardware detection failed: {exc}")


def _autostart():
    """Bring up every camera marked enabled, so a host reboot restores them."""
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
async def auth_state(request: Request):
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

    auth.set_credentials(username, password)
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
    if not auth.verify(username, password):
        return JSONResponse(
            {"detail": "Wrong username or password."}, status_code=401
        )
    return _set_session(JSONResponse({"authenticated": True, "username": username}))


@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.post("/api/auth/password")
async def auth_password(request: Request):
    payload = await request.json()
    current = str(payload.get("current", ""))
    new = str(payload.get("new", ""))
    if not auth.verify(auth.username(), current):
        return JSONResponse({"detail": "The current password is wrong."}, status_code=403)
    if len(new) < 8:
        return JSONResponse(
            {"detail": "The new password needs at least 8 characters."}, status_code=400
        )
    # This invalidates every existing session, including this one.
    auth.set_credentials(auth.username(), new)
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
async def index():
    with open(os.path.join(TEMPLATE_DIR, "index.html"), encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@app.get("/api/status")
async def status():
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
async def list_cameras():
    return [_decorate(cam) for cam in store.list()]


@app.post("/api/cameras")
async def create_camera(request: Request):
    payload = await request.json()
    draft = dict(models.DEFAULTS)
    draft.update(models.sanitize(payload))
    errors = models.validate(draft)
    if errors:
        return JSONResponse({"detail": " ".join(errors)}, status_code=400)

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
async def delete_camera(cam_id: str):
    cam = _get_or_404(cam_id)
    if manager is not None:
        try:
            manager.remove(cam)
        except DockerError as exc:
            print(f"[controller] cleanup of '{cam['name']}' failed: {exc}")
    store.delete(cam_id)
    return {"deleted": cam_id}


@app.post("/api/cameras/{cam_id}/{action}")
async def camera_action(cam_id: str, action: str):
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
async def camera_logs(cam_id: str, tail: int = 200):
    cam = _get_or_404(cam_id)
    return _require_manager().logs(cam, tail=tail)


@app.get("/api/cameras/{cam_id}/password")
async def camera_password(cam_id: str):
    """Returned only on explicit request, so the list view stays free of secrets."""
    cam = _get_or_404(cam_id)
    return {"password": cam.get("password", "")}


@app.get("/api/hwaccel")
async def hwaccel_state():
    if detector is None:
        return {"detected_at": 0, "summary": "Docker unavailable", "available": {}}
    report = dict(detector.cached())
    report["summary"] = summarize(report)
    return report


@app.post("/api/hwaccel/detect")
async def hwaccel_detect():
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
async def download_backup():
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
async def list_orphans():
    mgr = _require_manager()
    known = {cam["id"] for cam in store.list()}
    return {"orphans": mgr.orphans(known)}


@app.delete("/api/orphans/{name}")
async def remove_orphan(name: str):
    _require_manager().remove_orphan(name)
    return {"removed": name}
