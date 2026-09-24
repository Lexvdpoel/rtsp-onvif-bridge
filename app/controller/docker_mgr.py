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
from docker.types import DeviceRequest, IPAMConfig, IPAMPool

from ..common import models

LABEL_MANAGED = "rtsp-onvif-bridge.managed"
LABEL_CAM_ID = "rtsp-onvif-bridge.camera-id"

# Docker's macvlan driver refuses to create a network without an IPv4 pool
# ("ipv4 pool is empty"), so the DHCP server cannot simply be left in charge by
# using the null IPAM driver. Instead the network is given a parking subnet that
# does not exist on the LAN: Docker hands each container a throwaway address
# from it, which the camera flushes before asking the real DHCP server for its
# address. Carrier-grade NAT space is used because it practically never clashes
# with a home or office LAN, so the throwaway address cannot collide with a real
# device during the second or two that it is configured.
PARKING_SUBNET = "100.127.255.0/24"


class DockerError(RuntimeError):
    pass


class DockerManager:
    def __init__(self):
        self.image = os.environ.get("BRIDGE_IMAGE", "rtsp-onvif-bridge:latest")
        self.network_name = os.environ.get("MACVLAN_NETWORK", "camlan")
        self.parent = os.environ.get("MACVLAN_PARENT", "eth0")
        self.subnet = os.environ.get("MACVLAN_SUBNET", "").strip() or PARKING_SUBNET
        self.gateway = os.environ.get("MACVLAN_GATEWAY", "").strip()
        self.ip_range = os.environ.get("MACVLAN_IP_RANGE", "").strip()
        self.ipam_mode = os.environ.get("MACVLAN_IPAM", "").strip().lower()
        self.state_dir = os.environ.get("STATE_DIR", "/state")
        # Set by the controller once it has a hardware report to consult.
        self.detector = None
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

    def ipam_config(self) -> IPAMConfig:
        """Addressing for the macvlan network.

        Always subnet-based: see PARKING_SUBNET for why the null IPAM driver is
        not an option here.
        """
        if self.ipam_mode == "null":
            print(
                "[controller] MACVLAN_IPAM=null is ignored: Docker's macvlan "
                f"driver requires an IPv4 pool. Using subnet {self.subnet}; "
                "the cameras still get their real address over DHCP."
            )
        pool = IPAMPool(
            subnet=self.subnet,
            gateway=self.gateway or None,
            iprange=self.ip_range or None,
        )
        return IPAMConfig(driver="default", pool_configs=[pool])

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

        try:
            self.client.networks.create(
                name=self.network_name,
                driver="macvlan",
                options={"parent": self.parent},
                ipam=self.ipam_config(),
            )
        except APIError as exc:
            raise DockerError(
                f"Could not create macvlan network '{self.network_name}' on "
                f"parent '{self.parent}': {exc.explanation or exc}. "
                f"Check that MACVLAN_PARENT names a real interface on the "
                f"Docker host (run 'ip -br link' there)."
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
        hwaccel = self.resolved_hwaccel(cam)
        extra = self._hwaccel_access(cam, hwaccel)
        environment = models.env_for(cam, self.state_dir)
        # "auto" is settled here, where the hardware report is available; the
        # camera container only ever sees a concrete encoder.
        environment["HWACCEL"] = hwaccel
        try:
            container = self.client.containers.create(
                image=self.image,
                name=models.container_name(cam),
                hostname=models.hostname_for(cam),
                environment=environment,
                network=self.network_name,
                mac_address=cam["mac"],
                cap_add=["NET_ADMIN", "NET_RAW"],
                restart_policy={"Name": "unless-stopped"},
                volumes={source: {"bind": self.state_dir, "mode": "rw"}},
                labels={LABEL_MANAGED: "true", LABEL_CAM_ID: cam["id"]},
                detach=True,
                **extra,
            )
        except APIError as exc:
            raise DockerError(
                f"Could not create container for '{cam['name']}': "
                f"{exc.explanation or exc}"
            ) from exc
        return container

    def resolved_hwaccel(self, cam: dict) -> str:
        """The concrete encoder this camera will use."""
        if self.detector is not None:
            return self.detector.resolve(cam)
        requested = cam.get("hwaccel", "auto")
        # Without a hardware report there is nothing to choose from.
        return "none" if requested == "auto" else requested

    @staticmethod
    def _hwaccel_access(cam: dict, hwaccel: str) -> dict:
        """Give the container the GPU it needs, and only when it needs one."""
        if cam.get("output_codec") in (None, "", "copy") or hwaccel in ("none", "auto"):
            return {}
        if hwaccel in ("vaapi", "qsv"):
            # Intel and AMD render nodes live here.
            return {"devices": ["/dev/dri:/dev/dri:rwm"]}
        if hwaccel == "nvenc":
            # Needs the NVIDIA container runtime installed on the host.
            return {"device_requests": [DeviceRequest(count=-1, capabilities=[["gpu"]])]}
        return {}

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
