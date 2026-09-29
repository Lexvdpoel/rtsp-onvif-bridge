"""Answer UniFi's discovery probes so the camera offers itself for adoption.

A factory UniFi camera does not wait for a token: it answers the broadcast that
UniFi consoles send on UDP 10001, and Protect then lists it under "ready to
adopt". This does the same thing, which is what turns adoption into clicking
Adopt rather than fetching a token by hand.

There are two generations of the protocol and a console only understands a reply
in the version it asked in, so both are answered. A v1 probe is `01 00 00 00` and
a v2 probe is `02 08 00 00`; the reply is a one-byte version, a one-byte command,
a two-byte payload length, and then fields of type (1 byte), length (2 bytes),
value. The body is the same either way.

Field numbers and the model identities come from the reverse-engineering in the
unifi-cam-proxy-redalert fork (MIT); see CREDITS.md.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

DISCOVERY_PORT = 10001

# Two generations of the protocol are in use, and a console understands a reply
# only in the version it asked in, so both are answered.
#   v1: probe 01 00 00 00, reply header 01 00
#   v2: probe 02 08 00 00, reply header 02 06 (commands 6, 9 and 11 are all seen
#       from real devices; 6 is the plain "here is what I am")
PROBE_V1 = b"\x01\x00\x00\x00"
PROBE_V2 = b"\x02\x08\x00\x00"
PROBE = PROBE_V1  # kept for callers that only know about v1
REPLY_HEADERS = {1: (1, 0), 2: (2, 6)}

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
MODEL = 20
MODEL_SHORT = 21
DEVICE_ID = 32
DEFAULT_CREDENTIALS = 44
PRIMARY_ADDRESS = 47


def console_address(value: str) -> tuple[str, int]:
    """Console host to announce to. A management port is ignored: discovery
    listens on 10001 whatever port the websocket later uses."""
    host = (value or "").strip()
    if host.startswith(("http://", "https://")):
        host = host.split("//", 1)[1]
    host = host.rstrip("/")
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    return host, DISCOVERY_PORT


def _field(field_id: int, data: bytes) -> bytes:
    return struct.pack(">BH", field_id, len(data)) + data


def probe_version(data: bytes) -> int | None:
    """Which protocol version a probe is asking in, or None if it is not one."""
    if data.startswith(PROBE_V1[:2]):
        return 1
    if data.startswith(PROBE_V2[:2]):
        return 2
    return None


def build_response(mac: str, ip: str, hostname: str, identity: dict,
                   firmware: str, uptime: int, https_port: int = 443,
                   version: int = 1) -> bytes:
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
        # Real devices name themselves here as well; a console showing a model
        # in its adoption list is reading these.
        _field(MODEL, identity["model"].replace("_", "-").encode()),
        _field(MODEL_SHORT, identity["display"].encode()),
        _field(DEFAULT_CREDENTIALS, struct.pack("B", 1)),
    ])
    header_version, command = REPLY_HEADERS.get(version, REPLY_HEADERS[1])
    return struct.pack(">BBH", header_version, command, len(payload)) + payload


class DiscoveryResponder(threading.Thread):
    """Offers the camera for adoption until a console takes it.

    Two ways, because one is not enough. Answering the broadcast probe covers a
    console on the same segment. A broadcast does not cross a router, so when a
    console address is known the same packet is also sent straight to it every
    few seconds — the equivalent of telling a real camera where its Protect host
    is, which is what Ubiquiti has you do for a camera on another VLAN.
    """

    ANNOUNCE_SECONDS = 10
    # Six announcements is a minute of silence; long enough to be sure.
    HINT_AFTER = 6

    def __init__(self, cfg, state, identity: dict, adoptable=lambda: True,
                 console: str = ""):
        super().__init__(name="unifi-discovery", daemon=True)
        self.cfg = cfg
        self.state = state
        self.identity = identity
        self.adoptable = adoptable
        self.console = (console or "").strip()
        self.answered = 0
        self.announced = 0
        self.error = ""
        self._nudged = False
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

            where = f"UDP {DISCOVERY_PORT}"
            if self.console:
                where += f", announcing to {console_address(self.console)[0]}"
            print(
                f"[unifi-discovery] offering {self.identity['model']} "
                f"({self.identity['platform']}, {self.identity['sysid']}) on {where}"
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

    def _packet(self, version: int = 1) -> bytes:
        return build_response(
            version=version,
            mac=self.state.mac or self.cfg.mac_hint,
            ip=self.state.ip,
            hostname=self.cfg.hostname,
            identity=self.identity,
            firmware=self.cfg.unifi_firmware,
            uptime=int(time.monotonic() - self._started_at),
        )

    def _nudge(self):
        """Say something useful once it is clear nothing is going to adopt us.

        Discovery wants the console on the same layer 2 segment. Across a router
        the announcements below are a best effort, and when they come to nothing
        the operator should hear why rather than watch a silent log.
        """
        if self._nudged or self.announced < self.HINT_AFTER:
            return
        self._nudged = True
        print(
            "[unifi-discovery] still not adopted after "
            f"{self.announced} announcements to {console_address(self.console)[0]}. "
            "Discovery normally needs the console on the same layer 2 network as "
            "the camera; a console behind a router may simply ignore this. Either "
            "put the cameras on the console's VLAN, or switch discovery off for "
            "this camera and adopt it with a token instead."
        )

    def _announce(self, sock):
        """Tell a console on another subnet that this camera is here.

        Sent in both protocol versions, because there is no probe to tell us
        which one this console speaks.
        """
        if not self.console or not self.state.ip or not self.adoptable():
            return
        host, port = console_address(self.console)
        if not host:
            return
        try:
            for version in (1, 2):
                sock.sendto(self._packet(version), (host, port))
            self.announced += 1
            if self.announced == 1:
                print(f"[unifi-discovery] announced to {host}:{port} (v1 and v2)")
            self._nudge()
        except OSError as exc:
            self.error = f"could not announce to {host}: {exc}"
            print(f"[unifi-discovery] {self.error}")
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)[:200]

    def _serve(self, sock):
        next_announce = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_announce:
                self._announce(sock)
                next_announce = now + self.ANNOUNCE_SECONDS
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return

            version = probe_version(data)
            if version is None:
                continue
            if not self.adoptable():
                # Already adopted; a real camera stops offering itself.
                continue
            if not self.state.ip:
                continue

            try:
                reply = self._packet(version)
            except Exception as exc:  # noqa: BLE001
                self.error = f"could not build the reply: {exc}"
                print(f"[unifi-discovery] {self.error}")
                continue

            try:
                sock.sendto(reply, addr)
                self.answered += 1
                print(f"[unifi-discovery] answered {addr[0]} (v{version})")
            except OSError as exc:
                print(f"[unifi-discovery] reply to {addr[0]} failed: {exc}")

    def stop(self):
        self._stop.set()
