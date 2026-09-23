"""Bring the virtual camera's macvlan interface up via DHCP.

The container is attached to a macvlan network with a fixed MAC address, so the
DHCP server sees a distinct client per camera and can hand out (or reserve) a
dedicated IP for each one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

IFACE = os.environ.get("CAM_IFACE", "eth0")
UDHCPC_SCRIPT = "/usr/local/share/udhcpc.script"


def _run(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def read_mac(iface: str = IFACE) -> str:
    try:
        with open(f"/sys/class/net/{iface}/address") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def read_ip(iface: str = IFACE) -> str:
    """Current IPv4 address on the interface, or '' when it has none yet."""
    proc = _run(["ip", "-4", "-o", "addr", "show", "dev", iface])
    for line in proc.stdout.splitlines():
        parts = line.split()
        if "inet" in parts:
            return parts[parts.index("inet") + 1].split("/")[0]
    return ""


def read_prefix(iface: str = IFACE) -> int:
    proc = _run(["ip", "-4", "-o", "addr", "show", "dev", iface])
    for line in proc.stdout.splitlines():
        parts = line.split()
        if "inet" in parts:
            cidr = parts[parts.index("inet") + 1]
            if "/" in cidr:
                return int(cidr.split("/")[1])
    return 24


def read_gateway(iface: str = IFACE) -> str:
    proc = _run(["ip", "-4", "route", "show", "default", "dev", iface])
    parts = proc.stdout.split()
    if "via" in parts:
        return parts[parts.index("via") + 1]
    return ""


def _udhcpc_cmd() -> list[str] | None:
    if shutil.which("udhcpc"):
        return ["udhcpc"]
    if shutil.which("busybox"):
        return ["busybox", "udhcpc"]
    return None


def start_dhcp(hostname: str, lease_file: str) -> subprocess.Popen | None:
    """Flush any Docker-assigned address and take the lease from the real DHCP server.

    Returns the long-lived udhcpc process (it stays up to renew the lease), or
    None when no DHCP client is available on this image.
    """
    base = _udhcpc_cmd()
    if base is None:
        print("[net] no udhcpc binary found; keeping the address Docker assigned")
        return None

    _run(["ip", "link", "set", "dev", IFACE, "up"])
    _run(["ip", "-4", "addr", "flush", "dev", IFACE])

    env = dict(os.environ, LEASE_FILE=lease_file)
    cmd = base + [
        "-i", IFACE,
        "-s", UDHCPC_SCRIPT,
        "-x", f"hostname:{hostname}",
        "-V", "rtsp-onvif-bridge",
        "-f",          # foreground, we supervise it ourselves
        "-t", "6",     # discover retries per attempt
        "-T", "3",     # seconds between retries
        "-A", "10",    # seconds before retrying after a failed round
    ]
    print(f"[net] starting DHCP on {IFACE} as '{hostname}'")
    return subprocess.Popen(cmd, env=env)


def wait_for_ip(timeout: float = 45.0, interval: float = 0.5) -> str:
    """Block until the interface has an IPv4 address (or the timeout expires)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = read_ip()
        if ip:
            return ip
        time.sleep(interval)
    return read_ip()
