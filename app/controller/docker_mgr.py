"""Creates and supervises one container per virtual camera.

Each camera container is attached to a macvlan network with its own fixed MAC
address, so the DHCP server on the LAN hands it its own IP. That is what makes
the virtual cameras look like separate physical devices to an NVR.
"""

from __future__ import annotations

import os
import socket

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.types import IPAMConfig, IPAMPool

from ..common import models

LABEL_MANAGED = "rtsp-onvif-bridge.managed"
LABEL_CAM_ID = "rtsp-onvif-bridge.camera-id"


class DockerError(RuntimeError):
    pass


class DockerManager:
    def __init__(self):
        self.image = os.environ.get("BRIDGE_IMAGE", "rtsp-onvif-bridge:latest")
        self.network_name = os.environ.get("MACVLAN_NETWORK", "camlan")
        self.parent = os.environ.get("MACVLAN_PARENT", "eth0")
        self.subnet = os.environ.get("MACVLAN_SUBNET", "").strip()
        self.gateway = os.environ.get("MACVLAN_GATEWAY", "").strip()
        self.ipam_mode = os.environ.get("MACVLAN_IPAM", "null").strip().lower()
        self.state_dir = os.environ.get("STATE_DIR", "/state")
        try:
            self.client = docker.from_env()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            raise DockerError(f"Cannot reach the Docker daemon: {exc}") from exc

    # ------------------------------------------------------------------ plumbing

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def _state_mount_source(self) -> str:
        """Host-side source of our own /state mount, so cameras share it.

        Falls back to the in-container path, which still works when the
        controller itself was started with a host bind of the same path.
        """
        try:
            me = self.client.containers.get(socket.gethostname())
        except (NotFound, APIError):
            return self.state_dir
        for mount in me.attrs.get("Mounts", []):
            if mount.get("Destination") == self.state_dir:
                return mount.get("Name") or mount.get("Source") or self.state_dir
        return self.state_dir

    def ensure_network(self) -> dict:
        """Create the macvlan network if it is missing; return a short summary."""
        try:
            network = self.client.networks.get(self.network_name)
            return {
                "name": self.network_name,
                "created": False,
                "driver": network.attrs.get("Driver"),
                "parent": network.attrs.get("Options", {}).get("parent", ""),
            }
        except NotFound:
            pass

        if self.ipam_mode == "null":
            # No Docker-side addressing at all: the DHCP server is authoritative.
            ipam = IPAMConfig(driver="null")
        else:
            if not self.subnet:
                raise DockerError(
                    "MACVLAN_IPAM=docker requires MACVLAN_SUBNET to be set."
                )
            pool = IPAMPool(subnet=self.subnet, gateway=self.gateway or None)
            ipam = IPAMConfig(driver="default", pool_configs=[pool])

        try:
            self.client.networks.create(
                name=self.network_name,
                driver="macvlan",
                options={"parent": self.parent},
                ipam=ipam,
            )
        except APIError as exc:
            raise DockerError(
                f"Could not create macvlan network '{self.network_name}' on "
                f"parent '{self.parent}': {exc.explanation or exc}"
            ) from exc
        return {
            "name": self.network_name,
            "created": True,
            "driver": "macvlan",
            "parent": self.parent,
        }

    # ---------------------------------------------------------------- containers

    def _container(self, cam: dict):
        try:
            return self.client.containers.get(models.container_name(cam))
        except NotFound:
            return None

    def status(self, cam: dict) -> dict:
        container = self._container(cam)
        if container is None:
            return {"exists": False, "state": "absent", "container": ""}
        return {
            "exists": True,
            "state": container.status,
            "container": container.name,
            "started_at": container.attrs.get("State", {}).get("StartedAt", ""),
            "restarts": container.attrs.get("RestartCount", 0),
        }

    def create(self, cam: dict):
        """(Re)create the camera container from the stored configuration."""
        self.ensure_network()
        self.remove(cam)

        try:
            self.client.images.get(self.image)
        except ImageNotFound as exc:
            raise DockerError(
                f"Image '{self.image}' not found. Build it first with "
                "'docker compose build'."
            ) from exc

        source = self._state_mount_source()
        try:
            container = self.client.containers.create(
                image=self.image,
                name=models.container_name(cam),
                hostname=models.hostname_for(cam),
                environment=models.env_for(cam, self.state_dir),
                network=self.network_name,
                mac_address=cam["mac"],
                cap_add=["NET_ADMIN", "NET_RAW"],
                restart_policy={"Name": "unless-stopped"},
                volumes={source: {"bind": self.state_dir, "mode": "rw"}},
                labels={LABEL_MANAGED: "true", LABEL_CAM_ID: cam["id"]},
                detach=True,
            )
        except APIError as exc:
            raise DockerError(
                f"Could not create container for '{cam['name']}': "
                f"{exc.explanation or exc}"
            ) from exc
        return container

    def start(self, cam: dict):
        container = self._container(cam)
        if container is None:
            container = self.create(cam)
        try:
            container.start()
        except APIError as exc:
            raise DockerError(
                f"Could not start '{cam['name']}': {exc.explanation or exc}"
            ) from exc

    def stop(self, cam: dict):
        container = self._container(cam)
        if container is None:
            return
        try:
            container.stop(timeout=10)
        except APIError as exc:
            raise DockerError(
                f"Could not stop '{cam['name']}': {exc.explanation or exc}"
            ) from exc

    def restart(self, cam: dict):
        """Recreate rather than restart, so configuration changes take effect."""
        self.remove(cam)
        self.start(cam)

    def remove(self, cam: dict):
        container = self._container(cam)
        if container is None:
            return
        try:
            container.remove(force=True)
        except NotFound:
            pass
        except APIError as exc:
            raise DockerError(
                f"Could not remove container for '{cam['name']}': "
                f"{exc.explanation or exc}"
            ) from exc
        self._clear_state_file(cam)

    def logs(self, cam: dict, tail: int = 200) -> str:
        container = self._container(cam)
        if container is None:
            return "(no container - the camera has not been started yet)"
        try:
            return container.logs(tail=tail, timestamps=False).decode("utf-8", "replace")
        except APIError as exc:
            return f"(could not read logs: {exc.explanation or exc})"

    def _clear_state_file(self, cam: dict):
        path = os.path.join(self.state_dir, f"{cam['id']}.json")
        try:
            os.remove(path)
        except OSError:
            pass

    # --------------------------------------------------------------- bulk helpers

    def orphans(self, known_ids: set[str]) -> list[str]:
        """Camera containers we manage whose configuration no longer exists."""
        try:
            containers = self.client.containers.list(
                all=True, filters={"label": f"{LABEL_MANAGED}=true"}
            )
        except APIError:
            return []
        return [
            c.name
            for c in containers
            if c.labels.get(LABEL_CAM_ID) not in known_ids
        ]

    def remove_orphan(self, name: str):
        try:
            self.client.containers.get(name).remove(force=True)
        except (NotFound, APIError):
            pass
