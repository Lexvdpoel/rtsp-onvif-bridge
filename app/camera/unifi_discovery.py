"""Answer UniFi's discovery probes so the camera offers itself for adoption.

A factory UniFi camera does not wait for a token: it answers the broadcast that
UniFi consoles send on UDP 10001, and Protect then lists it under "ready to
adopt". This does the same thing, which is what turns adoption into clicking
Adopt rather than fetching a token by hand.

The probe is four bytes, `01 00 00 00`. The reply is a one-byte version, a
one-byte command, a two-byte payload length, and then fields of
type (1 byte), length (2 bytes), value.

Field numbers and the model identities come from the reverse-engineering in the
unifi-cam-proxy-redalert fork (MIT); see CREDITS.md.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

DISCOVERY_PORT = 10001
PROBE = b"\x01\x00\x00\x00"
VERSION = 1
CMD_INFO = 0

# Field identifiers in the discovery TLV.
HWADDR = 1
FWVERSION = 3
UPTIME = 10
HOSTNAME = 11
PLATFORM = 12
ESSID = 13
WMODE = 14
WEBUI = 15
SYSTEM_ID = 16
DEVICE_ID = 32
DEFAULT_CREDENTIALS = 44
PRIMARY_ADDRESS = 47


def _field(field_id: int, data: bytes) -> bytes:
    return struct.pack(">BH", field_id, len(data)) + data


def build_response(mac: str, ip: str, hostname: str, identity: dict,
                   firmware: str, uptime: int, https_port: int = 443) -> bytes:
    """The packet a UniFi camera sends back to a discovery probe."""
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(mac_bytes) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    ip_bytes = socket.inet_aton(ip)

    payload = b"".join([
        _field(PRIMARY_ADDRESS, mac_bytes + ip_bytes),
        _field(HWADDR, mac_bytes),
        _field(HOSTNAME, hostname.encode()),
        _field(PLATFORM, identity["platform"].encode()),
        _field(WMODE, struct.pack("B", 1)),          # wired
        _field(ESSID, b""),
        _field(FWVERSION, firmware.encode()),
        _field(DEVICE_ID, mac.encode()),
        _field(UPTIME, struct.pack(">I", uptime & 0xFFFFFFFF)),
        # protocol flag then port: 1 means the management UI speaks HTTPS.
        _field(WEBUI, struct.pack(">HH", 1, https_port)),
        _field(SYSTEM_ID, struct.pack("<H", int(identity["sysid"], 0))),
        _field(DEFAULT_CREDENTIALS, struct.pack("B", 1)),
    ])
    return struct.pack(">BBH", VERSION, CMD_INFO, len(payload)) + payload


class DiscoveryResponder(threading.Thread):
    """Replies to discovery probes for as long as the camera is unadopted."""

    def __init__(self, cfg, state, identity: dict, adoptable=lambda: True):
        super().__init__(name="unifi-discovery", daemon=True)
        self.cfg = cfg
        self.state = state
        self.identity = identity
        self.adoptable = adoptable
        self.answered = 0
        self.error = ""
        self._started_at = time.monotonic()
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind(("0.0.0.0", DISCOVERY_PORT))
                sock.settimeout(1.0)
            except OSError as exc:
                self.error = f"cannot listen on UDP {DISCOVERY_PORT}: {exc}"
                print(f"[unifi-discovery] {self.error}")
                if self._stop.wait(10):
                    return
                continue

            print(
                f"[unifi-discovery] announcing as {self.identity['model']} "
                f"({self.identity['platform']}, {self.identity['sysid']}) "
                f"on UDP {DISCOVERY_PORT}"
            )
            try:
                self._serve(sock)
            except Exception as exc:  # noqa: BLE001 - restart rather than die
                self.error = str(exc)[:200]
                print(f"[unifi-discovery] restarting after error: {exc}")
            finally:
                sock.close()
            if self._stop.wait(5):
                return

    def _serve(self, sock):
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return

            if not data.startswith(PROBE):
                continue
            if not self.adoptable():
                # Already adopted; a real camera stops offering itself.
                continue
            if not self.state.ip:
                continue

            try:
                reply = build_response(
                    mac=self.state.mac or self.cfg.mac_hint,
                    ip=self.state.ip,
                    hostname=self.cfg.hostname,
                    identity=self.identity,
                    firmware=self.cfg.unifi_firmware,
                    uptime=int(time.monotonic() - self._started_at),
                )
            except Exception as exc:  # noqa: BLE001
                self.error = f"could not build the reply: {exc}"
                print(f"[unifi-discovery] {self.error}")
                continue

            try:
                sock.sendto(reply, addr)
                self.answered += 1
                print(f"[unifi-discovery] answered {addr[0]}")
            except OSError as exc:
                print(f"[unifi-discovery] reply to {addr[0]} failed: {exc}")

    def stop(self):
        self._stop.set()
