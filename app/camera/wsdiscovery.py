"""WS-Discovery responder.

This is what makes a virtual camera show up in the "scan for ONVIF devices"
screen of an NVR. It listens for Probe messages on the WS-Discovery multicast
group and answers with a ProbeMatch pointing at this camera's device service.

ONVIF uses the 2005/04 WS-Discovery namespace together with the 2004/08
WS-Addressing namespace, not the later W3C versions.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET

MULTICAST_GROUP = "239.255.255.250"
MULTICAST_PORT = 3702

WSDD = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"

NS_ATTRS = (
    'xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" '
    f'xmlns:wsa="{WSA}" '
    f'xmlns:d="{WSDD}" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
)

DEVICE_TYPES = "dn:NetworkVideoTransmitter tds:Device"

# Type filters we consider a match for a video transmitter.
MATCHING_TYPES = ("networkvideotransmitter", "device", "onvif")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


class DiscoveryResponder(threading.Thread):
    def __init__(self, cfg, state):
        super().__init__(name="ws-discovery", daemon=True)
        self.cfg = cfg
        self.state = state
        self.urn = f"urn:uuid:{cfg.uuid}"
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ sockets

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.bind(("", MULTICAST_PORT))

        membership = struct.pack(
            "4s4s",
            socket.inet_aton(MULTICAST_GROUP),
            socket.inet_aton(self.state.ip or "0.0.0.0"),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        if self.state.ip:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self.state.ip),
            )
        sock.settimeout(1.0)
        return sock

    # ----------------------------------------------------------------- messages

    def _scopes(self) -> str:
        cfg = self.cfg
        safe = lambda v: "".join(  # noqa: E731 - tiny local helper
            ch if ch.isalnum() or ch in "-_" else "_" for ch in (v or "any")
        )
        return " ".join(
            [
                "onvif://www.onvif.org/type/video_encoder",
                "onvif://www.onvif.org/type/Network_Video_Transmitter",
                "onvif://www.onvif.org/Profile/Streaming",
                f"onvif://www.onvif.org/name/{safe(cfg.name)}",
                f"onvif://www.onvif.org/hardware/{safe(cfg.model)}",
                f"onvif://www.onvif.org/location/{safe(cfg.location)}",
            ]
        )

    def _xaddrs(self) -> str:
        return f"http://{self.state.ip}:{self.cfg.onvif_port}/onvif/device_service"

    def _probe_match(self, relates_to: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<SOAP-ENV:Envelope {NS_ATTRS}>"
            "<SOAP-ENV:Header>"
            f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
            f"<wsa:RelatesTo>{relates_to}</wsa:RelatesTo>"
            "<wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>"
            f"<wsa:Action>{WSDD}/ProbeMatches</wsa:Action>"
            "</SOAP-ENV:Header>"
            "<SOAP-ENV:Body><d:ProbeMatches><d:ProbeMatch>"
            f"<wsa:EndpointReference><wsa:Address>{self.urn}</wsa:Address>"
            "</wsa:EndpointReference>"
            f"<d:Types>{DEVICE_TYPES}</d:Types>"
            f"<d:Scopes>{self._scopes()}</d:Scopes>"
            f"<d:XAddrs>{self._xaddrs()}</d:XAddrs>"
            "<d:MetadataVersion>1</d:MetadataVersion>"
            "</d:ProbeMatch></d:ProbeMatches></SOAP-ENV:Body></SOAP-ENV:Envelope>"
        ).encode("utf-8")

    def _announce(self, kind: str) -> bytes:
        body = (
            f"<d:{kind}>"
            f"<wsa:EndpointReference><wsa:Address>{self.urn}</wsa:Address>"
            "</wsa:EndpointReference>"
            f"<d:Types>{DEVICE_TYPES}</d:Types>"
            f"<d:Scopes>{self._scopes()}</d:Scopes>"
            f"<d:XAddrs>{self._xaddrs()}</d:XAddrs>"
            "<d:MetadataVersion>1</d:MetadataVersion>"
            f"</d:{kind}>"
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<SOAP-ENV:Envelope {NS_ATTRS}>"
            "<SOAP-ENV:Header>"
            f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
            "<wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>"
            f"<wsa:Action>{WSDD}/{kind}</wsa:Action>"
            "</SOAP-ENV:Header>"
            f"<SOAP-ENV:Body>{body}</SOAP-ENV:Body></SOAP-ENV:Envelope>"
        ).encode("utf-8")

    # -------------------------------------------------------------------- logic

    @staticmethod
    def _parse_probe(data: bytes) -> tuple[str, str] | None:
        """Return (message_id, types) for a Probe, or None for anything else."""
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return None

        message_id = ""
        types = ""
        is_probe = False
        for node in root.iter():
            name = _localname(node.tag)
            if name == "MessageID":
                message_id = (node.text or "").strip()
            elif name == "Probe":
                is_probe = True
            elif name == "Types":
                types = (node.text or "").strip()
        return (message_id, types) if is_probe else None

    def _matches(self, types: str) -> bool:
        if not types.strip():
            return True  # an unfiltered probe matches every device
        lowered = types.lower()
        return any(candidate in lowered for candidate in MATCHING_TYPES)

    def run(self):
        while not self._stop.is_set():
            try:
                self._sock = self._open_socket()
            except OSError as exc:
                print(f"[discovery] cannot bind UDP {MULTICAST_PORT}: {exc}")
                if self._stop.wait(10):
                    return
                continue

            print(f"[discovery] listening on {MULTICAST_GROUP}:{MULTICAST_PORT}")
            self._send_multicast(self._announce("Hello"))

            try:
                self._loop()
            except Exception as exc:
                print(f"[discovery] restarting after error: {exc}")
            finally:
                self._close()
            if self._stop.wait(5):
                return

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return

            probe = self._parse_probe(data)
            if probe is None:
                continue
            message_id, types = probe
            if not self._matches(types):
                continue
            if not self.state.ip:
                continue

            try:
                self._sock.sendto(self._probe_match(message_id), addr)
                print(f"[discovery] answered probe from {addr[0]}")
            except OSError as exc:
                print(f"[discovery] reply to {addr[0]} failed: {exc}")

    def _send_multicast(self, payload: bytes):
        if not self._sock:
            return
        try:
            self._sock.sendto(payload, (MULTICAST_GROUP, MULTICAST_PORT))
        except OSError as exc:
            print(f"[discovery] multicast send failed: {exc}")

    def _close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def stop(self):
        """Say goodbye so clients drop the device promptly, then shut down."""
        self._stop.set()
        self._send_multicast(self._announce("Bye"))
        time.sleep(0.2)
        self._close()
