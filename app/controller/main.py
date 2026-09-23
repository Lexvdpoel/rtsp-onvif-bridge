"""Web UI and REST API for managing the virtual ONVIF cameras."""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ..common import models
from .docker_mgr import DockerError, DockerManager
from .store import CameraStore

DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

# A camera writes its state file every 10s; anything older means it went away.
STATE_STALE_SECONDS = 45

store = CameraStore(DATA_DIR)
manager: DockerManager | None = None
startup_error: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager, startup_error
    try:
        manager = DockerManager()
        manager.ensure_network()
    except DockerError as exc:
        startup_error = str(exc)
        print(f"[controller] {exc}")
    else:
        _autostart()
    yield


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


@app.get("/api/orphans")
async def list_orphans():
    mgr = _require_manager()
    known = {cam["id"] for cam in store.list()}
    return {"orphans": mgr.orphans(known)}


@app.delete("/api/orphans/{name}")
async def remove_orphan(name: str):
    _require_manager().remove_orphan(name)
    return {"removed": name}
